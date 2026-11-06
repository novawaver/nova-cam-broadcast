"""Ubuntu Broadcast Client entry point: GUI by default, ``--cli`` for headless use."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings import ClientSettings  # noqa: E402
from shared.logging_utils import setup_logging  # noqa: E402


def run_cli(args, settings: ClientSettings) -> int:
    from connection_manager import ConnectionManager

    both = not (args.camera or args.mic)
    manager = ConnectionManager(
        settings,
        lambda name, state, detail: print(f"{name}: {state.value} {detail}".rstrip(), flush=True),
        lambda name, message: print(f"[{name}] {message}", flush=True),
    )
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    if both or args.camera:
        manager.camera.start()
    if both or args.mic:
        manager.microphone.start()
    print("Running. Press Ctrl+C to stop.", flush=True)
    done.wait()
    manager.shutdown()
    return 0


def run_diagnose(settings: ClientSettings) -> int:
    from connection_manager import run_diagnostics

    checks, inputs = run_diagnostics(settings)
    for check in checks:
        print(f"[{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    print("Input devices:", *inputs or ["(none)"], sep="\n  ")
    return 0 if all(c.ok for c in checks) else 1


def run_gui(settings: ClientSettings) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from gui import MainWindow
    from shared.config import APP_NAME
    from shared.widgets import acquire_single_instance, make_icon

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("nvidia-broadcast-bridge")
    app.setWindowIcon(make_icon())

    lock = acquire_single_instance("client")
    if lock is None:
        QMessageBox.information(None, APP_NAME, "The client is already running.")
        return 0

    window = MainWindow(settings)
    app.aboutToQuit.connect(window.shutdown)  # also covers session logout

    # Python delivers signals only while its interpreter runs, so a timer keeps
    # it ticking inside Qt's event loop. Otherwise SIGTERM would skip cleanup.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(500)

    window.show()
    code = app.exec()
    del lock
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="NVIDIA Broadcast Bridge: Ubuntu client")
    parser.add_argument("--cli", action="store_true", help="run without the GUI")
    parser.add_argument("--camera", action="store_true", help="CLI: receive only the camera")
    parser.add_argument("--mic", action="store_true", help="CLI: receive only the microphone")
    parser.add_argument("--diagnose", action="store_true", help="check the system and exit")
    parser.add_argument("--host", help="CLI: Windows host, overriding settings for this run")
    args = parser.parse_args()

    setup_logging("client")
    settings = ClientSettings.load()
    if args.host:
        settings.host = args.host
    if args.diagnose:
        return run_diagnose(settings)
    if args.cli:
        return run_cli(args, settings)
    return run_gui(settings)


if __name__ == "__main__":
    sys.exit(main())
