"""Persistent settings for the Windows client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from shared.config import DEFAULT_CAMERA_PORT, JsonSettings


@dataclass
class ClientSettings(JsonSettings):
    FILE_NAME: ClassVar[str] = "windows-client.json"

    host: str = "10.0.2.2"  # the Windows host as seen through VirtualBox NAT
    camera_port: int = DEFAULT_CAMERA_PORT
    ffmpeg_path: str = ""
    check_for_updates: bool = True
    skipped_version: str = ""
