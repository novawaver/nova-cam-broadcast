"""Receive the camera stream and feed it to the virtual camera."""
from __future__ import annotations

import threading
from typing import Optional

from frame_writer import FPS, HEIGHT, WIDTH, FrameProcess, FrameWriter, frame_path
from shared.process import FFmpegProcess, base_ffmpeg_args
from shared.receiver import DeviceError, ReceiverWorker
from shared.stream_worker import LogCallback, StateCallback

CAMERA = "Camera"


def build_command(ffmpeg: str, host: str, port: int) -> list[str]:
    return [
        *base_ffmpeg_args(ffmpeg, progress=False),
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-probesize", "1000000",
        "-analyzeduration", "500000",
        # timeout covers connect and read, in microseconds. Without it a
        # silently dead network would block FFmpeg forever.
        "-i", f"tcp://{host}:{port}?timeout=10000000",
        "-an", "-sn",
        "-vf", f"scale={WIDTH}:{HEIGHT},fps={FPS},format=nv12",
        "-f", "rawvideo",
        "pipe:1",
    ]


class CameraReceiver(ReceiverWorker):
    missing_ffmpeg_message = "FFmpeg not found. Install it, or set its path in Settings."

    def __init__(self, settings, on_state: StateCallback, on_log: LogCallback) -> None:
        super().__init__(CAMERA, settings, on_state, on_log)
        self._writer: Optional[FrameWriter] = None

    def port(self) -> int:
        return self.settings.camera_port

    def prepare(self) -> None:
        if self._writer is not None:
            return
        try:
            self._writer = FrameWriter(frame_path())
        except FileNotFoundError:
            raise DeviceError("The camera isn't installed. Click Install Camera.") from None
        except (OSError, ValueError) as exc:
            raise DeviceError(f"Could not open the camera's frame file: {exc}") from exc

    def build_command(self, ffmpeg: str) -> list[str]:
        return build_command(ffmpeg, self.settings.host, self.settings.camera_port)

    def create_process(self, command: list[str]) -> FFmpegProcess:
        return FrameProcess(command, self._writer, lambda line: self._log(line))

    def connected_detail(self, ffmpeg: FFmpegProcess) -> str:
        fps = f"{ffmpeg.fps:.0f}" if ffmpeg.fps else str(FPS)
        return f"{WIDTH}x{HEIGHT} @ {fps} FPS"

    def _run(self, stop: threading.Event) -> None:
        try:
            super()._run(stop)
        finally:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
