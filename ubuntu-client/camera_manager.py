"""Virtual camera handling: v4l2loopback detection, creation and pixel formats."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from shared.process import base_ffmpeg_args
from shared.receiver import DeviceError

log = logging.getLogger(__name__)

HELPER = "/usr/local/libexec/nvbb-camera-helper"
_MODULE_SYS = Path("/sys/module/v4l2loopback")

# V4L2 fourcc to FFmpeg pix_fmt, in order of preference. YU12 and YUYV are the
# formats browsers read most reliably.
PIXEL_FORMATS = {"YU12": "yuv420p", "YUYV": "yuyv422", "NV12": "nv12", "RGB3": "rgb24", "BGR3": "bgr24"}
_FOURCC = re.compile(r"\[\d+\]:\s+'(\w{3,4})\s*'")


def parse_output_formats(text: str) -> list[str]:
    """Return FFmpeg pix_fmts from ``v4l2-ctl --list-formats-out``, best first."""
    found = _FOURCC.findall(text)
    return [pix for fourcc, pix in PIXEL_FORMATS.items() if fourcc in found]


class CameraManager:
    def __init__(self, device: str) -> None:
        self.device = device

    @property
    def _sys_path(self) -> Path:
        return Path("/sys/class/video4linux") / Path(self.device).name

    def exists(self) -> bool:
        return Path(self.device).exists()

    def is_loopback(self) -> bool:
        """v4l2loopback devices are virtual; real cameras sit on a hardware bus."""
        if not (self._sys_path.exists() and _MODULE_SYS.exists()):
            return False
        return "/virtual/" in os.path.realpath(self._sys_path)

    def label(self) -> str:
        try:
            return (self._sys_path / "name").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def ensure(self) -> None:
        """Make the device usable, loading the module only if truly necessary.

        A correctly configured device is reused untouched. Nothing here ever
        unloads or reloads the module, which other applications may be using.
        """
        if not self.exists():
            self._create()
        if not self.is_loopback():
            raise DeviceError(f"{self.device} exists but is not a v4l2loopback device.")
        if not os.access(self.device, os.W_OK):
            raise DeviceError(
                f"No write access to {self.device}. Add yourself to the 'video' group "
                "(sudo usermod -aG video $USER), then log out and back in."
            )

    def _create(self) -> None:
        """Run the one fixed root helper, instead of elevating the whole GUI."""
        if not Path(HELPER).exists():
            raise DeviceError(f"{self.device} is missing. Run scripts/install_ubuntu.sh first.")
        pkexec = shutil.which("pkexec")
        if not pkexec:
            raise DeviceError("pkexec is not installed, so the camera driver cannot be loaded.")
        try:
            result = subprocess.run(
                [pkexec, HELPER], stdin=subprocess.DEVNULL, capture_output=True,
                text=True, errors="replace", timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise DeviceError("Timed out waiting for authorization.") from None
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise DeviceError(f"Could not create {self.device}: {detail}")
        if not self.exists():
            raise DeviceError(f"The helper ran but {self.device} did not appear.")

    def candidate_formats(self) -> list[str]:
        """FFmpeg pix_fmts to try, best first. Defaults if detection is unavailable."""
        default = ["yuv420p", "yuyv422"]
        v4l2_ctl = shutil.which("v4l2-ctl")
        if not v4l2_ctl:
            return default
        try:
            result = subprocess.run(
                [v4l2_ctl, "-d", self.device, "--list-formats-out"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return default
        return parse_output_formats(result.stdout) or default


def input_url(host: str, port: int) -> str:
    # timeout covers connect and read, in microseconds. Without it a silently
    # dead network would block FFmpeg forever.
    return f"tcp://{host}:{port}?timeout=10000000"


def build_command(ffmpeg: str, s, pix_fmt: str) -> list[str]:
    return [
        *base_ffmpeg_args(ffmpeg),
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-probesize", "1000000",
        "-analyzeduration", "500000",
        "-i", input_url(s.host, s.camera_port),
        "-an", "-sn",
        "-vf", f"scale={s.width}:{s.height},fps={s.fps},format={pix_fmt}",
        "-f", "v4l2",
        s.video_device,
    ]
