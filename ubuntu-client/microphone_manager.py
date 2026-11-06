"""Virtual microphone handling through the PulseAudio protocol.

Works on both PipeWire (pipewire-pulse) and classic PulseAudio:

    FFmpeg -> null sink "nvbb_mic_sink" -> monitor -> remap source "nvbb_mic"

The remap source is required, not cosmetic: browsers such as Chrome hide sink
monitors from their microphone list, so the monitor alone would not be
selectable. A null sink has no hardware output, so the incoming audio is never
played on the speakers and cannot feed back.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from camera_manager import DeviceError
from shared.config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from shared.process import base_ffmpeg_args

log = logging.getLogger(__name__)

SINK_NAME = "nvbb_mic_sink"
SOURCE_NAME = "nvbb_mic"
MONITOR_NAME = f"{SINK_NAME}.monitor"
INTERNAL_SINK_LABEL = "NVIDIA Broadcast Bridge (internal)"
PLAYBACK_BUFFER_MS = "60"


@dataclass
class Module:
    id: str
    name: str
    args: str


@dataclass
class Source:
    name: str
    description: str
    is_monitor: bool


def _names_module(args: str, key: str, value: str) -> bool:
    return re.search(rf"""(^|\s){key}=["']?{re.escape(value)}["']?(\s|$)""", args) is not None


def parse_modules(text: str) -> list[Module]:
    """Parse ``pactl list short modules``: id<TAB>name<TAB>args<TAB>..."""
    modules = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            modules.append(Module(parts[0].strip(), parts[1].strip(), parts[2] if len(parts) > 2 else ""))
    return modules


def parse_sources(text: str) -> list[Source]:
    """Parse the long ``pactl list sources`` output (run with LC_ALL=C)."""
    sources: list[Source] = []
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("Source #"):
            sources.append(Source("", "", False))
        elif not sources:
            continue
        elif stripped.startswith("Name: "):
            sources[-1].name = stripped[6:]
        elif stripped.startswith("Description: "):
            sources[-1].description = stripped[13:]
        elif stripped.startswith("Monitor of Sink: "):
            sources[-1].is_monitor = stripped[17:] != "n/a"
    return sources


def _properties(description: str, icon: str = "") -> str:
    # Two quoting levels: the module-argument parser strips the outer double
    # quotes and the property parser the inner single quotes. Spaces in the
    # description would otherwise end the argument.
    safe = description.replace("'", "").replace('"', "")
    extra = f" device.icon_name='{icon}'" if icon else ""
    return f"\"device.description='{safe}'{extra}\""


class MicrophoneManager:
    def __init__(self, description: str, on_log: Callable[[str], None] = lambda _: None) -> None:
        self.description = description
        self._log = on_log
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return shutil.which("pactl") is not None

    def _pactl(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["pactl", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=10, env={**os.environ, "LC_ALL": "C"},
            )
        except FileNotFoundError:
            raise DeviceError("pactl is missing. Install pulseaudio-utils.") from None
        except subprocess.TimeoutExpired:
            raise DeviceError("The audio server did not respond.") from None
        if result.returncode != 0:
            raise DeviceError(f"pactl {args[0]}: {result.stderr.strip() or 'failed'}")
        return result.stdout

    def server_name(self) -> str:
        for line in self._pactl("info").splitlines():
            if line.startswith("Server Name:"):
                return line.split(":", 1)[1].strip()
        return "unknown"

    def sources(self) -> list[Source]:
        return parse_sources(self._pactl("list", "sources"))

    def _short_names(self, kind: str) -> set[str]:
        names = set()
        for line in self._pactl("list", "short", kind).splitlines():
            parts = line.split("\t")
            if len(parts) > 1:
                names.add(parts[1].strip())
        return names

    def _owned_modules(self) -> tuple[list[Module], list[Module]]:
        """Find modules by our unique names. Nothing else is ever touched."""
        modules = parse_modules(self._pactl("list", "short", "modules"))
        sinks = [m for m in modules if m.name == "module-null-sink"
                 and _names_module(m.args, "sink_name", SINK_NAME)]
        sources = [m for m in modules if m.name == "module-remap-source"
                   and _names_module(m.args, "source_name", SOURCE_NAME)]
        return sinks, sources

    def _unload(self, module: Module) -> None:
        try:
            self._pactl("unload-module", module.id)
        except DeviceError as exc:
            log.warning("Could not unload module %s: %s", module.id, exc)

    def _default_sink(self) -> Optional[str]:
        try:
            return self._pactl("get-default-sink").strip() or None
        except DeviceError:
            return None

    def _wait_for(self, kind: str, name: str) -> bool:
        """PipeWire can publish a new node slightly after load-module returns."""
        for _ in range(20):
            if name in self._short_names(kind):
                return True
            time.sleep(0.1)
        return False

    def ensure(self) -> None:
        """Create or reuse the virtual microphone. Safe to call repeatedly.

        Modules left by a crashed session are reused and any duplicates
        removed, so restarts never multiply the microphone.
        """
        with self._lock:
            previous_default = self._default_sink()
            sinks, remaps = self._owned_modules()
            for extra in sinks[1:] + remaps[1:]:
                self._unload(extra)
            remaps = remaps[:1]

            if SINK_NAME not in self._short_names("sinks"):
                for stale in remaps:  # a remap without its master is dead
                    self._unload(stale)
                remaps = []
                self._load_sink()

            # Decided by the live source, not the module list, which may omit
            # arguments: that case must reuse the source, not add a second one.
            if SOURCE_NAME not in self._short_names("sources"):
                for stale in remaps:
                    self._unload(stale)
                self._load_source()

            # The new sink must never become the default output, or desktop
            # audio would go silent. Default input is left as the user set it.
            if previous_default and previous_default != SINK_NAME:
                if self._default_sink() == SINK_NAME:
                    self._pactl("set-default-sink", previous_default)
                    self._log(f"Restored default output: {previous_default}")

    def _load_sink(self) -> None:
        self._pactl(
            "load-module", "module-null-sink",
            f"sink_name={SINK_NAME}",
            "format=s16le", f"rate={AUDIO_SAMPLE_RATE}",
            f"channels={AUDIO_CHANNELS}", "channel_map=mono",
            f"sink_properties={_properties(INTERNAL_SINK_LABEL)}",
        )
        if not self._wait_for("sinks", SINK_NAME):
            raise DeviceError("The virtual audio sink did not appear.")

    def _load_source(self) -> None:
        try:
            self._pactl(
                "load-module", "module-remap-source",
                f"master={MONITOR_NAME}",
                f"source_name={SOURCE_NAME}",
                f"channels={AUDIO_CHANNELS}", "channel_map=mono",
                f"source_properties={_properties(self.description, 'audio-input-microphone')}",
            )
            if not self._wait_for("sources", SOURCE_NAME):
                raise DeviceError("the source did not appear")
        except DeviceError as exc:
            self._log(
                f"Could not create a dedicated microphone ({exc}). Using the sink monitor, "
                "which some apps hide. Select it in their settings or with pavucontrol."
            )
            return
        found = [s for s in self.sources() if s.name == SOURCE_NAME]
        if found and found[0].description != self.description:
            self._log(f"Microphone is listed as '{found[0].description}'")

    def cleanup(self) -> None:
        """Remove our modules: the source first, because it depends on the sink."""
        with self._lock:
            try:
                sinks, remaps = self._owned_modules()
            except DeviceError as exc:
                log.warning("Skipping audio cleanup: %s", exc)
                return
            for module in remaps + sinks:
                self._unload(module)


def input_url(host: str, port: int) -> str:
    return f"tcp://{host}:{port}?timeout=10000000"


def build_command(ffmpeg: str, s) -> list[str]:
    return [
        *base_ffmpeg_args(ffmpeg),
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-probesize", "32",
        "-analyzeduration", "0",
        "-f", "s16le",
        "-ar", str(AUDIO_SAMPLE_RATE),
        "-ac", str(AUDIO_CHANNELS),
        "-i", input_url(s.host, s.mic_port),
        "-c:a", "pcm_s16le",
        "-f", "pulse",
        "-device", SINK_NAME,
        "-buffer_duration", PLAYBACK_BUFFER_MS,
        "NVIDIA Broadcast Bridge",
    ]
