"""Persistent settings and launch-at-login for the Windows server."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from shared.config import (
    DEFAULT_CAMERA_PORT, DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_MIC_PORT,
    DEFAULT_WIDTH, JsonSettings,
)

DEFAULT_CAMERA = "Camera (NVIDIA Broadcast)"
DEFAULT_MICROPHONE = "Microphone (NVIDIA Broadcast)"

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "NvidiaBroadcastBridgeServer"


@dataclass
class ServerSettings(JsonSettings):
    FILE_NAME: ClassVar[str] = "server.json"

    camera_device: str = DEFAULT_CAMERA
    microphone_device: str = DEFAULT_MICROPHONE
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    video_quality: int = 5  # FFmpeg -q:v: 2 = best, 31 = worst
    camera_port: int = DEFAULT_CAMERA_PORT
    mic_port: int = DEFAULT_MIC_PORT
    # 0.0.0.0 is the proven setup. See README "Security" before changing it.
    bind_address: str = "0.0.0.0"
    ffmpeg_path: str = ""
    minimize_to_tray: bool = False


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    main_py = Path(__file__).resolve().with_name("main.py")
    return subprocess.list2cmdline([str(pythonw), str(main_py), "--minimized"])


def launch_at_startup_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
        return True
    except OSError:
        return False


def set_launch_at_startup(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_VALUE)
            except FileNotFoundError:
                pass
