"""Persistent settings for the Ubuntu client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from shared.config import (
    DEFAULT_CAMERA_PORT, DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_MIC_PORT,
    DEFAULT_WIDTH, JsonSettings,
)


@dataclass
class ClientSettings(JsonSettings):
    FILE_NAME: ClassVar[str] = "client.json"

    host: str = "10.0.2.2"  # the Windows host as seen through VirtualBox NAT
    camera_port: int = DEFAULT_CAMERA_PORT
    mic_port: int = DEFAULT_MIC_PORT
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    video_device: str = "/dev/video10"
    microphone_name: str = "NVIDIA Broadcast Microphone"
    ffmpeg_path: str = ""
