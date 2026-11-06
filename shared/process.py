"""FFmpeg discovery and subprocess management, with no GUI dependency.

Orphan prevention is handled by the operating system, so it still works if the
application crashes or is force-killed:

* Windows: children join a Job Object with KILL_ON_JOB_CLOSE.
* Linux: children set PR_SET_PDEATHSIG, so the parent's death sends SIGTERM.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional, Sequence

from shared.config import WINDOWS

log = logging.getLogger(__name__)

# FFmpeg -progress keys that only advance when media really flows.
_DATA_KEYS = ("frame", "total_size", "out_time_us")


def find_ffmpeg(configured: str = "") -> Optional[str]:
    """Locate ffmpeg: explicit setting, next to the app, PATH, common folders."""
    exe = "ffmpeg.exe" if WINDOWS else "ffmpeg"
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / exe)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    if WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        for candidate in (
            local / "Microsoft" / "WinGet" / "Links" / exe,
            program_files / "ffmpeg" / "bin" / exe,
            Path(r"C:\ffmpeg\bin") / exe,
            Path(r"C:\ProgramData\chocolatey\bin") / exe,
            Path.home() / "scoop" / "shims" / exe,
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def base_ffmpeg_args(ffmpeg: str) -> list[str]:
    """Arguments common to every stream.

    ``-progress pipe:1`` provides the data-flow signal used for real connection
    state. Windows keeps stdin open so a graceful ``q`` can be sent; on Linux
    SIGTERM does that job, so stdin is disabled.
    """
    args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning", "-progress", "pipe:1"]
    if not WINDOWS:
        args.append("-nostdin")
    return args


def hidden_run_kwargs() -> dict:
    """subprocess kwargs that stop a console window flashing on Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


# --- orphan prevention --------------------------------------------------------

_job_lock = threading.Lock()
_job_handle = None


def _create_kill_on_close_job():
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def _assign_to_job(process: subprocess.Popen) -> None:
    """Tie a child to this app's lifetime. Failure is logged, never fatal."""
    global _job_handle
    import ctypes
    from ctypes import wintypes

    try:
        with _job_lock:
            if _job_handle is None:
                _job_handle = _create_kill_on_close_job()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not kernel32.AssignProcessToJobObject(_job_handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as exc:
        log.warning("Could not attach FFmpeg to job object: %s", exc)


_libc = None
if sys.platform == "linux":
    import ctypes as _ctypes

    # Loaded up front: preexec_fn must not import or dlopen after fork().
    _libc = _ctypes.CDLL("libc.so.6", use_errno=True)


def _die_with_parent() -> None:
    PR_SET_PDEATHSIG = 1
    _libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


# --- process wrapper ----------------------------------------------------------


class FFmpegProcess:
    """One running FFmpeg with its output drained on background threads.

    PDEATHSIG fires when the *thread* that forked exits, so on Linux ``start``
    must be called from a thread that outlives the process. StreamWorker's
    supervising thread satisfies that.
    """

    def __init__(self, args: Sequence[str], on_line: Optional[Callable[[str], None]] = None) -> None:
        self.args = list(args)
        self._on_line = on_line
        self._proc: Optional[subprocess.Popen] = None
        self._readers: list[threading.Thread] = []
        self._tail: deque[str] = deque(maxlen=40)
        self._counters: dict[str, str] = {}
        self.started_at = 0.0
        self.last_data: Optional[float] = None  # monotonic time media last advanced
        self.fps: Optional[float] = None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    @property
    def tail(self) -> list[str]:
        return list(self._tail)

    def start(self) -> "FFmpegProcess":
        """Spawn FFmpeg. Raises OSError if the executable cannot be started."""
        kwargs: dict = {
            "stdin": subprocess.PIPE if WINDOWS else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if WINDOWS:
            kwargs.update(hidden_run_kwargs())
        elif _libc is not None:
            kwargs["preexec_fn"] = _die_with_parent
        self._proc = subprocess.Popen(self.args, **kwargs)
        self.started_at = time.monotonic()
        if WINDOWS:
            _assign_to_job(self._proc)
        for target in (self._read_stderr, self._read_progress):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._readers.append(thread)
        return self

    def poll(self) -> Optional[int]:
        return self._proc.poll() if self._proc else None

    def _read_stderr(self) -> None:
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self._tail.append(line)
            if self._on_line:
                self._on_line(line)

    def _read_progress(self) -> None:
        for raw in self._proc.stdout:
            key, sep, value = raw.decode("ascii", "replace").strip().partition("=")
            if not sep:
                continue
            value = value.strip()
            if key == "fps":
                try:
                    self.fps = float(value)
                except ValueError:
                    pass
            elif key in _DATA_KEYS and value.isdigit() and int(value) > 0:
                if self._counters.get(key) != value:
                    self._counters[key] = value
                    self.last_data = time.monotonic()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop gracefully, escalating to a kill. Safe to call repeatedly."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if WINDOWS:
                    # Ignored while FFmpeg is blocked in accept(), hence the
                    # kill below. That is harmless: nothing is being written.
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                else:
                    proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    log.error("FFmpeg pid %s did not exit after kill", proc.pid)
        for thread in self._readers:
            thread.join(1)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
