"""Logging setup and FFmpeg log-noise suppression."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time

from shared.config import config_dir


def setup_logging(app_name: str, verbose: bool = False) -> None:
    """Log to a rotating file in the config directory and to stderr."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    log_dir = config_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / f"{app_name}.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # sys.stderr is None in a windowed PyInstaller build; StreamHandler would
    # then fail on every record.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


class RateLimiter:
    """Allow a repeating log line through at most once per ``interval`` seconds."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = -interval

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False
