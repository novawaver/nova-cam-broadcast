"""Tests for parsing, command construction and supervision.

These need no camera, FFmpeg, PipeWire or GUI, and run on either OS.
"""
import sys
import threading
import time
from types import SimpleNamespace

import camera_manager
import microphone_manager
import stream_manager
from connection_manager import ReceiverWorker, classify_failure
from device_manager import parse_dshow_devices
from microphone_manager import SINK_NAME, SOURCE_NAME, _properties, parse_modules, parse_sources
from shared.stream_worker import Backoff, StreamState

MODERN = """
[dshow @ 0000020] "Camera (NVIDIA Broadcast)" (video)
[dshow @ 0000020]   Alternative name "@device_sw_{860BB310}\\{ABC}"
[dshow @ 0000020] "OBS (Virtual) Camera" (none)
[dshow @ 0000020] "Capture Card" (video, audio)
[dshow @ 0000020] "Microphone (NVIDIA Broadcast)" (audio)
[dshow @ 0000020]   Alternative name "@device_cm_{33D9A762}\\wave_{DEF}"
dummy: Immediate exit requested
"""

LEGACY = """
[dshow @ 0000020] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000020]  "Camera (NVIDIA Broadcast)"
[dshow @ 0000020]     Alternative name "@device_sw_{ABC}"
[dshow @ 0000020] DirectShow audio devices
[dshow @ 0000020]  "Microphone (NVIDIA Broadcast)"
"""


def test_parses_current_dshow_listing():
    devices = parse_dshow_devices(MODERN)
    assert devices.video == ["Camera (NVIDIA Broadcast)", "Capture Card"]
    assert devices.audio == ["Capture Card", "Microphone (NVIDIA Broadcast)"]


def test_parses_legacy_dshow_listing():
    devices = parse_dshow_devices(LEGACY)
    assert devices.video == ["Camera (NVIDIA Broadcast)"]
    assert devices.audio == ["Microphone (NVIDIA Broadcast)"]


def server_settings():
    return SimpleNamespace(
        width=1280, height=720, fps=30, video_quality=5, camera_port=9000, mic_port=9001,
        bind_address="0.0.0.0", camera_device="Camera (NVIDIA Broadcast)",
        microphone_device="Microphone (NVIDIA Broadcast)",
    )


def client_settings():
    return SimpleNamespace(
        host="10.0.2.2", camera_port=9000, mic_port=9001, width=1280, height=720, fps=30,
        video_device="/dev/video10",
    )


def test_camera_command_preserves_the_proven_transport():
    cmd = stream_manager.build_camera_command("ffmpeg", server_settings())
    assert cmd[cmd.index("-c:v") + 1] == "mpeg2video"
    assert cmd[cmd.index("-q:v") + 1] == "5"
    assert cmd[cmd.index("-i") + 1] == "video=Camera (NVIDIA Broadcast)"
    assert cmd[-3:] == ["-f", "mpegts", "tcp://0.0.0.0:9000?listen=1"]


def test_microphone_command_bounds_the_capture_buffer():
    cmd = stream_manager.build_microphone_command("ffmpeg", server_settings())
    assert cmd.index("-rtbufsize") < cmd.index("-i"), "must apply to the input"
    assert cmd[-3:] == ["-f", "s16le", "tcp://0.0.0.0:9001?listen=1"]
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"


def test_client_camera_command():
    cmd = camera_manager.build_command("ffmpeg", client_settings(), "yuyv422")
    assert cmd[cmd.index("-vf") + 1] == "scale=1280:720,fps=30,format=yuyv422"
    assert cmd[-3:] == ["-f", "v4l2", "/dev/video10"]
    assert cmd[cmd.index("-i") + 1].startswith("tcp://10.0.2.2:9000?timeout=")


def test_client_microphone_command_targets_the_null_sink():
    cmd = microphone_manager.build_command("ffmpeg", client_settings())
    assert cmd.index("-f") < cmd.index("-i"), "raw PCM needs its format before the input"
    assert cmd[cmd.index("-device") + 1] == SINK_NAME


def test_error_summaries():
    assert "not found" in stream_manager.summarize_error(
        ["Could not find video device with name [X] among source devices of type video."]
    )
    assert "resolution" in stream_manager.summarize_error(["Could not set video options"])
    assert stream_manager.summarize_error([]) == "FFmpeg exited unexpectedly."


def test_v4l2_format_parsing_prefers_browser_friendly_formats():
    text = """ioctl: VIDIOC_ENUM_FMT
    Type: Video Output

    [0]: 'RGB3' (24-bit RGB 8-8-8)
    [1]: 'YUYV' (YUYV 4:2:2)
    [2]: 'YU12' (Planar YUV 4:2:0)
"""
    assert camera_manager.parse_output_formats(text) == ["yuv420p", "yuyv422", "rgb24"]
    assert camera_manager.parse_output_formats("nothing here") == []


def test_module_parsing_and_ownership_are_exact():
    text = (
        f"536870912\tmodule-null-sink\tsink_name={SINK_NAME} rate=48000\n"
        "536870913\tmodule-null-sink\tsink_name=nvbb_mic_sink_other\n"
        f"536870914\tmodule-remap-source\tmaster={SINK_NAME}.monitor source_name={SOURCE_NAME}\n"
    )
    manager = microphone_manager.MicrophoneManager("Mic")
    manager._pactl = lambda *args: text
    sinks, sources = manager._owned_modules()
    assert [m.id for m in sinks] == ["536870912"]
    assert [m.id for m in sources] == ["536870914"]
    assert len(parse_modules(text)) == 3


def test_ensure_reuses_modules_from_a_previous_session_and_removes_duplicates():
    calls = []
    modules = (
        f"1\tmodule-null-sink\tsink_name={SINK_NAME}\n"
        f"2\tmodule-null-sink\tsink_name={SINK_NAME}\n"
        f"3\tmodule-remap-source\tsource_name={SOURCE_NAME}\n"
    )

    def pactl(*args):
        calls.append(args)
        if args == ("list", "short", "modules"):
            return modules
        if args == ("list", "short", "sinks"):
            return f"0\t{SINK_NAME}\tmodule-null-sink.c\ts16le 1ch 48000Hz\tIDLE\n"
        if args == ("list", "short", "sources"):
            return f"0\t{SOURCE_NAME}\tmodule-remap-source.c\ts16le 1ch 48000Hz\tIDLE\n"
        if args == ("get-default-sink",):
            return "speakers\n"
        return ""

    manager = microphone_manager.MicrophoneManager("Mic")
    manager._pactl = pactl
    manager.ensure()
    assert ("unload-module", "2") in calls
    assert not any(c[0] == "load-module" for c in calls)


def test_ensure_restores_the_default_output_if_the_new_sink_takes_it():
    calls = []
    state = {"loaded": False, "default": "speakers"}

    def pactl(*args):
        calls.append(args)
        if args == ("list", "short", "modules"):
            return ""
        if args == ("get-default-sink",):
            return state["default"] + "\n"
        if args == ("list", "short", "sinks"):
            return f"0\t{SINK_NAME}\tx\ty\tIDLE\n" if state["loaded"] else ""
        if args == ("list", "short", "sources"):
            return f"0\t{SOURCE_NAME}\tx\ty\tIDLE\n" if state["loaded"] else ""
        if args[0] == "load-module":
            state["loaded"] = True
            state["default"] = SINK_NAME
        return ""

    manager = microphone_manager.MicrophoneManager("Mic")
    manager._pactl = pactl
    manager.ensure()
    assert ("set-default-sink", "speakers") in calls


def test_properties_quote_spaces_for_both_parsers():
    assert _properties("NVIDIA Broadcast Microphone") == (
        "\"device.description='NVIDIA Broadcast Microphone'\""
    )


def test_source_parsing_flags_monitors():
    text = """Source #1
\tName: nvbb_mic
\tDescription: NVIDIA Broadcast Microphone
\tMonitor of Sink: n/a
Source #2
\tName: nvbb_mic_sink.monitor
\tDescription: Monitor of internal
\tMonitor of Sink: nvbb_mic_sink
"""
    sources = parse_sources(text)
    assert [(s.name, s.is_monitor) for s in sources] == [
        ("nvbb_mic", False), ("nvbb_mic_sink.monitor", True),
    ]
    assert sources[0].description == "NVIDIA Broadcast Microphone"


def test_failure_classification():
    assert classify_failure(["Connection refused"], "h", 1) == (
        "Windows server is not listening on h:1", False,
    )
    assert classify_failure(["Connection timed out"], "h", 1)[1] is False
    assert classify_failure(["/dev/video10: Permission denied"], "h", 1)[1] is True


def test_backoff_doubles_caps_and_resets():
    backoff = Backoff()
    assert [backoff.next() for _ in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    backoff.reset()
    assert backoff.next() == 1


FAKE_FFMPEG = r"""
import sys, time
print("frame=1", flush=True)
time.sleep(0.2)
print("frame=2", flush=True)
time.sleep(float(sys.argv[1]))
"""


class FakeReceiver(ReceiverWorker):
    """Drives the real supervision loop with a stand-in for FFmpeg."""

    runtime = 0.1

    def __init__(self, on_state, on_log):
        settings = SimpleNamespace(host="h", ffmpeg_path="")
        super().__init__("Fake", settings, on_state, on_log)
        self.attempts = 0

    def ffmpeg_path(self):
        return sys.executable

    def port(self):
        return 1

    def prepare(self):
        self.attempts += 1

    def build_command(self, ffmpeg):
        return [ffmpeg, "-c", FAKE_FFMPEG, str(self.runtime)]


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_worker_reports_connected_then_reconnects_after_the_stream_ends():
    states = []
    worker = FakeReceiver(lambda _, state, __: states.append(state), lambda *_: None)
    assert worker.start()
    assert not worker.start(), "a second start must not spawn a duplicate"
    assert wait_until(lambda: worker.attempts >= 2), "should retry after exit"
    worker.stop(wait=True)
    assert StreamState.CONNECTED in states
    assert StreamState.RECONNECTING in states
    assert states[-1] == StreamState.STOPPED


def test_stopping_one_worker_leaves_the_other_running():
    events = threading.Event()
    first = FakeReceiver(lambda *_: None, lambda *_: None)
    second = FakeReceiver(
        lambda _, state, __: state == StreamState.CONNECTED and events.set(), lambda *_: None,
    )
    first.runtime = second.runtime = 30
    first.start()
    second.start()
    assert events.wait(10)
    first.stop(wait=True)
    assert not first.running
    assert second.running
    assert second.state == StreamState.CONNECTED
    second.stop(wait=True)
    assert not second.running
