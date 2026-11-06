"""Receive the two streams, reconnect after failures, and run diagnostics.

There is deliberately no TCP "ping" to test whether the server is reachable.
The server uses FFmpeg's ``listen=1``, which accepts exactly one connection: a
probe would be that connection, end the server's FFmpeg and cost a restart.
Reachability is learned from the errors of the real connection attempt.
"""
from __future__ import annotations

import abc
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import camera_manager
import microphone_manager
from camera_manager import CameraManager, DeviceError
from microphone_manager import MONITOR_NAME, SOURCE_NAME, MicrophoneManager
from shared.config import AUDIO_SAMPLE_RATE
from shared.process import FFmpegProcess, find_ffmpeg
from shared.stream_worker import Backoff, LogCallback, StateCallback, StreamState, StreamWorker

log = logging.getLogger(__name__)

CAMERA = "Camera"
MICROPHONE = "Microphone"

POLL_SECONDS = 0.25
# Longer than FFmpeg's own 10 s socket timeout, which should normally fire
# first. This catches a stall in the local output instead.
STALL_SECONDS = 15.0
# A connection this healthy resets the retry delay.
STABLE_SECONDS = 10.0


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
    if "timed out" in text.lower():
        return f"No response from {host}:{port}", False
    if "No route to host" in text or "Network is unreachable" in text:
        return f"Network unreachable: {host}", False
    return "Stream ended", False


class ReceiverWorker(StreamWorker):
    """Keeps one stream connected, with exponential backoff between attempts."""

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

    def _run(self, stop: threading.Event) -> None:
        ffmpeg_path = self.ffmpeg_path()
        if not ffmpeg_path:
            self._set_state(StreamState.ERROR, "FFmpeg not found. Run the installer.")
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
            ffmpeg = FFmpegProcess(self.build_command(ffmpeg_path), lambda s: self._log(s))
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


class CameraReceiver(ReceiverWorker):
    def __init__(self, settings, on_state: StateCallback, on_log: LogCallback) -> None:
        super().__init__(CAMERA, settings, on_state, on_log)
        self._formats: list[str] = []
        self._format_index = 0

    def port(self) -> int:
        return self.settings.camera_port

    def reset(self) -> None:
        self._formats = []
        self._format_index = 0

    def prepare(self) -> None:
        device = CameraManager(self.settings.video_device)
        device.ensure()
        if not self._formats:
            self._formats = device.candidate_formats()
            self._log(f"Using {self._formats[0]} on {self.settings.video_device}")

    def build_command(self, ffmpeg: str) -> list[str]:
        return camera_manager.build_command(ffmpeg, self.settings, self._formats[self._format_index])

    def on_failure(self, lines: list[str]) -> bool:
        """The loopback device may be locked to another format; try the next one."""
        rejected = any("VIDIOC_" in line for line in lines)
        if rejected and self._format_index + 1 < len(self._formats):
            self._format_index += 1
            self._log(f"Format rejected; trying {self._formats[self._format_index]}")
            return True
        return False

    def connected_detail(self, ffmpeg: FFmpegProcess) -> str:
        s = self.settings
        fps = f"{ffmpeg.fps:.0f}" if ffmpeg.fps else str(s.fps)
        return f"{s.width}x{s.height} @ {fps} FPS"


class MicrophoneReceiver(ReceiverWorker):
    def __init__(self, settings, mic: MicrophoneManager,
                 on_state: StateCallback, on_log: LogCallback) -> None:
        super().__init__(MICROPHONE, settings, on_state, on_log)
        self.mic = mic

    def port(self) -> int:
        return self.settings.mic_port

    def prepare(self) -> None:
        self.mic.ensure()

    def build_command(self, ffmpeg: str) -> list[str]:
        return microphone_manager.build_command(ffmpeg, self.settings)

    def connected_detail(self, ffmpeg: FFmpegProcess) -> str:
        return f"{AUDIO_SAMPLE_RATE} Hz mono"


class ConnectionManager:
    """Owns both receivers. Starting or stopping one never touches the other."""

    def __init__(self, settings, on_state: StateCallback, on_log: LogCallback) -> None:
        self.settings = settings
        self.mic = MicrophoneManager(
            settings.microphone_name, lambda message: on_log(MICROPHONE, message)
        )
        self.camera = CameraReceiver(settings, on_state, on_log)
        self.microphone = MicrophoneReceiver(settings, self.mic, on_state, on_log)

    def start_all(self) -> None:
        self.camera.start()
        self.microphone.start()

    def stop_all(self) -> None:
        self.camera.stop()
        self.microphone.stop()

    def shutdown(self) -> None:
        """Stop everything and remove our audio modules.

        /dev/video10 stays: it is a kernel device that other applications may
        hold open, and the project rules forbid unloading the module.
        """
        self.stop_all()
        self.camera.stop(wait=True)
        self.microphone.stop(wait=True)
        if self.mic.available():
            self.mic.cleanup()


# --- diagnostics --------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _ffmpeg_lists(ffmpeg: str, flag: str, word: str) -> bool:
    try:
        output = subprocess.run(
            [ffmpeg, "-hide_banner", flag], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    # The flag columns before the name vary between FFmpeg versions.
    return any(word in line.split()[:3] for line in output.splitlines())


def run_diagnostics(settings) -> tuple[list[Check], list[str]]:
    """Return (checks, available input devices). May block; call off the GUI thread."""
    checks: list[Check] = []

    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    checks.append(Check("FFmpeg", bool(ffmpeg), ffmpeg or "not found"))
    if ffmpeg:
        for muxer, purpose in (("v4l2", "virtual camera"), ("pulse", "virtual microphone")):
            supported = _ffmpeg_lists(ffmpeg, "-muxers", muxer)
            checks.append(Check(f"FFmpeg {muxer} output", supported,
                                purpose if supported else f"missing; {purpose} cannot work"))

    camera = CameraManager(settings.video_device)
    module_loaded = Path("/sys/module/v4l2loopback").exists()
    modinfo = shutil.which("modinfo") or "/usr/sbin/modinfo"
    installed = module_loaded
    if not installed:
        try:
            installed = subprocess.run(
                [modinfo, "v4l2loopback"], stdin=subprocess.DEVNULL,
                capture_output=True, timeout=10,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            installed = False
    checks.append(Check("v4l2loopback", installed,
                        "loaded" if module_loaded else ("installed" if installed else "not installed")))

    if camera.exists():
        if camera.is_loopback():
            checks.append(Check(settings.video_device, True, camera.label() or "v4l2loopback"))
        else:
            checks.append(Check(settings.video_device, False, "exists but is not v4l2loopback"))
    else:
        checks.append(Check(settings.video_device, False, "missing"))

    inputs: list[str] = []
    mic = MicrophoneManager(settings.microphone_name)
    if not mic.available():
        checks.append(Check("PulseAudio/PipeWire", False, "pactl not found"))
        return checks, inputs
    try:
        checks.append(Check("PulseAudio/PipeWire", True, mic.server_name()))
        sources = mic.sources()
        inputs = [f"{s.description or s.name} ({s.name})" for s in sources if not s.is_monitor]
        names = {s.name for s in sources}
        if SOURCE_NAME in names:
            checks.append(Check("Virtual microphone", True, settings.microphone_name))
        elif MONITOR_NAME in names:
            checks.append(Check("Virtual microphone", True, "monitor fallback; some apps hide it"))
        else:
            checks.append(Check("Virtual microphone", False, "created on Connect Microphone"))
    except DeviceError as exc:
        checks.append(Check("PulseAudio/PipeWire", False, str(exc)))
    return checks, inputs
