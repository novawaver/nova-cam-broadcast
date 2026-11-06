"""Windows Broadcast Server entry point: GUI by default, ``--cli`` for headless use."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings import ServerSettings  # noqa: E402
from shared.logging_utils import setup_logging  # noqa: E402
from shared.process import find_ffmpeg  # noqa: E402


def run_cli(args, settings: ServerSettings) -> int:
    from stream_manager import StreamManager

    both = not (args.camera or args.mic)
    manager = StreamManager(
        settings,
        lambda name, state, detail: print(f"{name}: {state.value} {detail}".rstrip(), flush=True),
        lambda name, message: print(f"[{name}] {message}", flush=True),
    )
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    if both or args.camera:
        manager.camera.start()
    if both or args.mic:
        manager.microphone.start()
    print("Streaming. Press Ctrl+C to stop.", flush=True)
    while not done.wait(0.5):  # a timed wait keeps Ctrl+C responsive on Windows
        pass
    manager.shutdown()
    return 0


def list_devices(settings: ServerSettings) -> int:
    from device_manager import list_devices as detect

    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    if not ffmpeg:
        print("FFmpeg not found.", file=sys.stderr)
        return 1
    devices = detect(ffmpeg)
    print("Cameras:", *devices.video or ["(none)"], sep="\n  ")
    print("Microphones:", *devices.audio or ["(none)"], sep="\n  ")
    return 0


def run_gui(args, settings: ServerSettings) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from gui import MainWindow
    from shared.config import APP_NAME
    from shared.widgets import acquire_single_instance, make_icon

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)

    lock = acquire_single_instance("server")
    if lock is None:
        QMessageBox.information(None, APP_NAME, "The server is already running.")
        return 0

    window = MainWindow(settings)
    if not (args.minimized and window.tray is not None):
        window.show()
    code = app.exec()
    del lock
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="NVIDIA Broadcast Bridge: Windows server")
    parser.add_argument("--cli", action="store_true", help="run without the GUI")
    parser.add_argument("--camera", action="store_true", help="CLI: stream only the camera")
    parser.add_argument("--mic", action="store_true", help="CLI: stream only the microphone")
    parser.add_argument("--list-devices", action="store_true", help="list devices and exit")
    parser.add_argument("--minimized", action="store_true", help="start in the system tray")
    args = parser.parse_args()

    setup_logging("server")
    settings = ServerSettings.load()
    if args.list_devices:
        return list_devices(settings)
    if args.cli:
        return run_cli(args, settings)
    return run_gui(args, settings)


if __name__ == "__main__":
    sys.exit(main())
