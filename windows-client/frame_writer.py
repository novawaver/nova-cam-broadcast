"""Write video frames into the file the virtual camera reads.

The layout must match windows-client/vcam/frame_file.h.
"""
from __future__ import annotations

import ctypes
import mmap
import os
import struct
import time
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from shared.process import FFmpegProcess

MAGIC = 0x4242564E  # "NVBB"
VERSION = 1
WIDTH = 1280
HEIGHT = 720
FPS = 30
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2  # NV12
SLOTS = 3
HEADER_BYTES = 4096
FILE_BYTES = HEADER_BYTES + FRAME_BYTES * SLOTS

# magic, version, width, height, latestSlot, reserved, sequence, updatedTickMs
_HEADER = struct.Struct("<IIIIIIQQ")

_FPS_WINDOW_SECONDS = 1.0


def data_dir() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "NvidiaBroadcastBridge"


def frame_path() -> Path:
    return data_dir() / "camera.frames"


def _tick_ms() -> int:
    """GetTickCount64, the clock the camera compares against. It is system-wide."""
    function = ctypes.windll.kernel32.GetTickCount64
    function.restype = ctypes.c_ulonglong
    return function()


class FrameWriter:
    """Cycles through the slots, publishing each frame once it is complete.

    A frame is written to a slot the camera isn't reading, and the header is
    updated last, so the camera never sees a half-written frame.
    """

    def __init__(self, path: Path, tick: Callable[[], int] = _tick_ms) -> None:
        self._tick = tick
        self._sequence = 0
        self._slot = 0
        self._file = open(path, "r+b")
        try:
            if os.fstat(self._file.fileno()).st_size < FILE_BYTES:
                raise ValueError("The frame file is the wrong size. Reinstall the camera.")
            self._map = mmap.mmap(self._file.fileno(), FILE_BYTES)
        except Exception:
            self._file.close()
            raise

    def next_slot(self) -> memoryview:
        """Return the buffer for the next frame. Release it before ``publish``."""
        self._slot = (self._slot + 1) % SLOTS
        start = HEADER_BYTES + self._slot * FRAME_BYTES
        with memoryview(self._map) as view:
            return view[start:start + FRAME_BYTES]

    def publish(self) -> None:
        self._sequence += 1
        self._map[:_HEADER.size] = _HEADER.pack(
            MAGIC, VERSION, WIDTH, HEIGHT, self._slot, 0, self._sequence, self._tick()
        )

    def close(self) -> None:
        self._map.close()
        self._file.close()


def read_exactly(stream: BinaryIO, buffer: memoryview) -> bool:
    """Fill ``buffer``. Returns False if the stream ends first."""
    filled = 0
    while filled < len(buffer):
        with buffer[filled:] as remaining:
            count = stream.readinto(remaining)
        if not count:
            return False
        filled += count
    return True


class FrameProcess(FFmpegProcess):
    """FFmpeg with raw frames on stdout, copied straight into the frame file.

    Stdout carries video, so FFmpeg's progress output isn't available. Each
    complete frame is the data-flow signal instead.
    """

    def __init__(self, args, writer: FrameWriter,
                 on_line: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(args, on_line)
        self._writer = writer

    def _read_stdout(self) -> None:
        window_start = time.monotonic()
        frames = 0
        while True:
            with self._writer.next_slot() as slot:
                complete = read_exactly(self._proc.stdout, slot)
            if not complete:
                return
            self._writer.publish()
            now = time.monotonic()
            self.last_data = now
            frames += 1
            if now - window_start >= _FPS_WINDOW_SECONDS:
                self.fps = frames / (now - window_start)
                window_start, frames = now, 0
