"""Keep one incoming stream connected, shared by every client.

There is deliberately no TCP "ping" to test whether the server is reachable.
The server uses FFmpeg's ``listen=1``, which accepts exactly one connection: a
probe would be that connection, end the server's FFmpeg and cost a restart.
Reachability is learned from the errors of the real connection attempt.
"""
from __future__ import annotations

import abc
import threading
import time
from typing import Optional

from shared.process import FFmpegProcess, find_ffmpeg
from shared.stream_worker import Backoff, LogCallback, StateCallback, StreamState, StreamWorker

POLL_SECONDS = 0.25
# Longer than FFmpeg's own 10 s socket timeout, which should normally fire
# first. This catches a stall in the local output instead.
STALL_SECONDS = 15.0
# A connection this healthy resets the retry delay.
STABLE_SECONDS = 10.0


class DeviceError(Exception):
    """A virtual device problem that retrying will not fix."""


def classify_failure(lines: list[str], host: str, port: int) -> tuple[str, bool]:
    """Return (message, fatal). Fatal problems need the user, not a retry."""
    text = "\n".join(lines)
    if "Permission denied" in text:
        return "Permission denied by the local device.", True
    # Checked first: a dead local audio server also says "Connection refused".
    if "pa_" in text:
        return "The local audio server rejected the stream", False
    if "Connection refused" in text:
        return f"Windows server is not listening on {host}:{port}", False
    # FFmpeg builds for Windows report some socket errors only as numbers.
    if "timed out" in text.lower() or "Error number -138" in text:
        return f"No response from {host}:{port}", False
    if "No route to host" in text or "Network is unreachable" in text:
        return f"Network unreachable: {host}", False
    return "Stream ended", False


class ReceiverWorker(StreamWorker):
    """Keeps one stream connected, with exponential backoff between attempts."""

    missing_ffmpeg_message = "FFmpeg not found. Run the installer."

    def __init__(self, name: str, settings, on_state: StateCallback, on_log: LogCallback) -> None:
        super().__init__(name, on_state, on_log)
        self.settings = settings

    @abc.abstractmethod
    def port(self) -> int:
        ...

    @abc.abstractmethod
    def prepare(self) -> None:
        """Make the local device ready. Raises DeviceError if it cannot be."""

    @abc.abstractmethod
    def build_command(self, ffmpeg: str) -> list[str]:
        ...

    def connected_detail(self, ffmpeg: FFmpegProcess) -> str:
        return ""

    def reset(self) -> None:
        """Clear per-session state before a new run."""

    def on_failure(self, lines: list[str]) -> bool:
        """Return True to retry immediately with a changed configuration."""
        return False

    def ffmpeg_path(self) -> Optional[str]:
        return find_ffmpeg(self.settings.ffmpeg_path)

    def create_process(self, command: list[str]) -> FFmpegProcess:
        return FFmpegProcess(command, lambda line: self._log(line))

    def _run(self, stop: threading.Event) -> None:
        ffmpeg_path = self.ffmpeg_path()
        if not ffmpeg_path:
            self._set_state(StreamState.ERROR, self.missing_ffmpeg_message)
            return

        self.reset()
        backoff = Backoff()
        host, port = self.settings.host, self.port()
        waiting_state = StreamState.STARTING

        while not stop.is_set():
            try:
                self.prepare()
            except DeviceError as exc:
                self._set_state(StreamState.ERROR, str(exc))
                return

            self._set_state(waiting_state, f"Connecting to {host}:{port}")
            ffmpeg = self.create_process(self.build_command(ffmpeg_path))
            try:
                ffmpeg.start()
            except OSError as exc:
                self._set_state(StreamState.ERROR, f"Could not start FFmpeg: {exc}")
                return
            try:
                connected_seconds = self._supervise(ffmpeg, stop)
            finally:
                ffmpeg.stop()

            if stop.is_set():
                return
            if connected_seconds >= STABLE_SECONDS:
                backoff.reset()
            lines = ffmpeg.tail
            if self.on_failure(lines):
                continue
            message, fatal = classify_failure(lines, host, port)
            if fatal:
                self._set_state(StreamState.ERROR, message)
                return

            delay = backoff.next()
            self._log(f"{message}; retrying in {delay:.0f} s")
            waiting_state = StreamState.RECONNECTING
            self._set_state(StreamState.RECONNECTING, f"{message}; retrying in {delay:.0f} s")
            stop.wait(delay)

    def _supervise(self, ffmpeg: FFmpegProcess, stop: threading.Event) -> float:
        """Report state until FFmpeg ends. Returns seconds spent connected.

        Connected means media is flowing, which a merely running FFmpeg does
        not prove.
        """
        connected_at: Optional[float] = None
        while not stop.wait(POLL_SECONDS):
            if ffmpeg.poll() is not None:
                break
            last = ffmpeg.last_data
            if last is None:
                continue
            if connected_at is None:
                connected_at = time.monotonic()
                self._log("Stream connected")
            self._set_state(StreamState.CONNECTED, self.connected_detail(ffmpeg))
            if time.monotonic() - last > STALL_SECONDS:
                self._log("Stream stalled")
                break
        return time.monotonic() - connected_at if connected_at else 0.0
