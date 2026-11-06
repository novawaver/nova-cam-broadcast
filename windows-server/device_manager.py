"""Enumerate DirectShow devices through FFmpeg."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from shared.process import hidden_run_kwargs

# Current format:  [dshow @ 0x..] "Camera (NVIDIA Broadcast)" (video)
_TYPED = re.compile(r'"(?P<name>[^"]+)"\s+\((?P<types>[^()]*)\)\s*$')
# Older format, grouped under section headers:  [dshow @ 0x..]  "Camera"
_UNTYPED = re.compile(r'\]\s+"(?P<name>[^"]+)"\s*$')


@dataclass
class DeviceList:
    video: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)


def parse_dshow_devices(text: str) -> DeviceList:
    devices = DeviceList()
    section = None

    def add(target: list[str], name: str) -> None:
        if name not in target:
            target.append(name)

    for line in text.splitlines():
        if "Alternative name" in line:
            continue
        lowered = line.lower()
        if "directshow video devices" in lowered:
            section = devices.video
            continue
        if "directshow audio devices" in lowered:
            section = devices.audio
            continue
        match = _TYPED.search(line)
        if match:
            types = {t.strip() for t in match["types"].split(",")}
            if "video" in types:
                add(devices.video, match["name"])
            if "audio" in types:
                add(devices.audio, match["name"])
            continue
        match = _UNTYPED.search(line)
        if match and section is not None:
            add(section, match["name"])
    return devices


def list_devices(ffmpeg: str) -> DeviceList:
    """Ask FFmpeg for devices. It exits non-zero by design, so that is ignored."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15, **hidden_run_kwargs(),
    )
    return parse_dshow_devices(result.stderr.decode("utf-8", "replace"))
