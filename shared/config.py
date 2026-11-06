"""Constants and JSON settings persistence shared by both applications."""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import ClassVar

APP_ID = "nvidia-broadcast-bridge"
APP_NAME = "NVIDIA Broadcast Bridge"

WINDOWS = sys.platform == "win32"

DEFAULT_CAMERA_PORT = 9000
DEFAULT_MIC_PORT = 9001
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 30
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1

log = logging.getLogger(__name__)


def config_dir() -> Path:
    """Return the per-user configuration directory, creating it if needed."""
    if WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class JsonSettings:
    """Dataclass persisted as JSON.

    Unknown keys, wrongly typed values and invalid ports are ignored on load so
    a damaged or outdated file can never stop the application from starting.
    """

    FILE_NAME: ClassVar[str] = "settings.json"

    @classmethod
    def path(cls) -> Path:
        return config_dir() / cls.FILE_NAME

    @classmethod
    def load(cls):
        obj = cls()
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return obj
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings file: %s", exc)
            return obj
        if not isinstance(raw, dict):
            return obj
        for field in fields(cls):
            if field.name in raw and type(raw[field.name]) is type(getattr(obj, field.name)):
                setattr(obj, field.name, raw[field.name])
        defaults = cls()
        for field in fields(cls):
            if field.name.endswith("_port") and not 1 <= getattr(obj, field.name) <= 65535:
                setattr(obj, field.name, getattr(defaults, field.name))
        return obj

    def save(self) -> None:
        """Write atomically so a crash mid-save cannot corrupt the file."""
        path = self.path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)
