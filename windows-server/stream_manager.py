"""Capture and serve the camera and microphone with independent FFmpeg processes."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import port_monitor
from shared.config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from shared.logging_utils import RateLimiter
from shared.process import FFmpegProcess, base_ffmpeg_args, find_ffmpeg
from shared.stream_worker import LogCallback, StateCallback, StreamState, StreamWorker

log = logging.getLogger(__name__)

CAMERA = "Camera"
MICROPHONE = "Microphone"

POLL_SECONDS = 0.5
# Connected but no media advancing: the capture device stalled or the client
# vanished without closing the socket (for example the VM was powered off).
STALL_SECONDS = 10.0
RESTART_DELAY = 0.5

# Bounded on purpose. The buffer fills while FFmpeg waits for a client and
# dshow drops the *newest* data when it is full, so whatever it holds is stale
# latency at connect time. The 3 MB default is about 30 s of 48 kHz mono audio.
AUDIO_RTBUFSIZE = "64k"
AUDIO_DEVICE_BUFFER_MS = "20"


def listen_url(host: str, port: int) -> str:
    return f"tcp://{host}:{port}?listen=1"


def build_camera_command(ffmpeg: str, s) -> list[str]:
    frame_bytes = s.width * s.height * 2  # YUY2, the usual capture format
    return [
        *base_ffmpeg_args(ffmpeg),
        "-f", "dshow",
        "-rtbufsize", str(frame_bytes * 3),
        "-video_size", f"{s.width}x{s.height}",
        "-framerate", str(s.fps),
        "-i", f"video={s.camera_device}",
        "-an",
        "-c:v", "mpeg2video",
        "-pix_fmt", "yuv420p",
        "-q:v", str(s.video_quality),
        "-flush_packets", "1",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-f", "mpegts",
        listen_url(s.bind_address, s.camera_port),
    ]


def build_microphone_command(ffmpeg: str, s) -> list[str]:
    return [
        *base_ffmpeg_args(ffmpeg),
        "-f", "dshow",
        "-rtbufsize", AUDIO_RTBUFSIZE,
        "-audio_buffer_size", AUDIO_DEVICE_BUFFER_MS,
        "-i", f"audio={s.microphone_device}",
        "-vn",
        "-ac", str(AUDIO_CHANNELS),
        "-ar", str(AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        # Without this, audio leaves in 32 KB chunks, about a third of a second.
        "-flush_packets", "1",
        "-f", "s16le",
        listen_url(s.bind_address, s.mic_port),
    ]


def summarize_error(tail: list[str]) -> str:
    """Turn FFmpeg's last output into a short user-facing reason."""
    text = "\n".join(tail)
    if ("Could not find" in text and "device" in text) or "none found" in text:
        return "Device not found. Is NVIDIA Broadcast running?"
    if "Could not set video options" in text or "Could not find video mode" in text:
        return "The camera does not support this resolution/FPS."
    if "Could not run graph" in text or "I/O error" in text:
        return "Could not open the device. It may be in use or disconnected."
    return tail[-1] if tail else "FFmpeg exited unexpectedly."


class ServerStream(StreamWorker):
    def __init__(self, name: str, port_of: Callable[[], int],
                 build: Callable[[str], list[str]], ffmpeg_of: Callable[[], Optional[str]],
                 on_state: StateCallback, on_log: LogCallback) -> None:
        super().__init__(name, on_state, on_log)
        self._port_of = port_of
        self._build = build
        self._ffmpeg_of = ffmpeg_of
        self._buffer_warning = RateLimiter(30)

    def _on_line(self, line: str) -> None:
        # dshow repeats this constantly while waiting for a client. Harmless.
        if "real-time buffer" in line and not self._buffer_warning.allow():
            return
        self._log(line)

    def _run(self, stop: threading.Event) -> None:
        ffmpeg_path = self._ffmpeg_of()
        if not ffmpeg_path:
            self._set_state(StreamState.ERROR, "FFmpeg not found. Set its path in Settings.")
            return
        port = self._port_of()

        while not stop.is_set():
            owner = port_monitor.listener_pid(port)
            if owner is not None:
                self._set_state(StreamState.ERROR, f"Port {port} is in use by process {owner}.")
                return

            self._set_state(StreamState.STARTING, "Opening device")
            ffmpeg = FFmpegProcess(self._build(ffmpeg_path), self._on_line)
            try:
                ffmpeg.start()
            except OSError as exc:
                self._set_state(StreamState.ERROR, f"Could not start FFmpeg: {exc}")
                return
            try:
                device_opened = self._supervise(ffmpeg, port, stop)
            finally:
                ffmpeg.stop()

            if stop.is_set():
                return
            if not device_opened:
                self._set_state(StreamState.ERROR, summarize_error(ffmpeg.tail))
                return
            # listen=1 serves a single client, so a new process is needed for
            # the next one. A device that vanished fails above on that restart.
            self._log("Stream ended; listening again")
            self._set_state(StreamState.WAITING, "Client disconnected")
            stop.wait(RESTART_DELAY)

    def _supervise(self, ffmpeg: FFmpegProcess, port: int, stop: threading.Event) -> bool:
        """Mirror the real socket state until FFmpeg ends.

        Returns True if the socket ever came up. FFmpeg opens the device before
        the socket, so that proves the device worked and any later exit is a
        lost client. Checking "was connected" instead would misreport a client
        that connects and drops between two polls as a device error.
        """
        device_opened = False
        was_connected = False
        connected_at = 0.0
        while not stop.wait(POLL_SECONDS):
            if ffmpeg.poll() is not None:
                break
            state = port_monitor.socket_state(port, ffmpeg.pid)
            if state == "listening":
                device_opened = True
                self._set_state(StreamState.WAITING, f"Port {port}")
            elif state == "connected":
                device_opened = True
                if not was_connected:
                    was_connected = True
                    connected_at = time.monotonic()
                    self._log("Ubuntu client connected")
                fps = f"{ffmpeg.fps:.0f} fps" if self.name == CAMERA and ffmpeg.fps else ""
                self._set_state(StreamState.CONNECTED, fps)
                # Armed only once progress has been seen, so a build that
                # reports none cannot cause a false restart.
                last = ffmpeg.last_data
                if last is not None and time.monotonic() - max(last, connected_at) > STALL_SECONDS:
                    self._log(f"No data for {STALL_SECONDS:.0f} s; dropping the connection")
                    break
        return device_opened


class StreamManager:
    """Owns the two streams. Starting or stopping one never touches the other."""

    def __init__(self, settings, on_state: StateCallback, on_log: LogCallback) -> None:
        self.settings = settings
        self.camera = ServerStream(
            CAMERA, lambda: self.settings.camera_port,
            lambda ff: build_camera_command(ff, self.settings), self._ffmpeg, on_state, on_log,
        )
        self.microphone = ServerStream(
            MICROPHONE, lambda: self.settings.mic_port,
            lambda ff: build_microphone_command(ff, self.settings), self._ffmpeg, on_state, on_log,
        )

    def _ffmpeg(self) -> Optional[str]:
        return find_ffmpeg(self.settings.ffmpeg_path)

    def start_all(self) -> None:
        self.camera.start()
        self.microphone.start()

    def stop_all(self) -> None:
        self.camera.stop()
        self.microphone.stop()

    def shutdown(self) -> None:
        self.stop_all()
        self.camera.stop(wait=True)
        self.microphone.stop(wait=True)
