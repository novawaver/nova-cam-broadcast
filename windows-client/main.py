"""Windows Broadcast Client entry point."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client_settings import ClientSettings  # noqa: E402
from shared.logging_utils import setup_logging  # noqa: E402


def run_gui(settings: ClientSettings) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from client_gui import MainWindow
    from shared import updater
    from shared.config import APP_NAME
    from shared.widgets import acquire_single_instance, make_icon

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_icon())

    lock = acquire_single_instance("windows-client")
    if lock is None:
        QMessageBox.information(None, APP_NAME, "The client is already running.")
        return 0

    window = MainWindow(settings)
    app.aboutToQuit.connect(window.shutdown)  # also covers session logout

    # Python delivers signals only while its interpreter runs, so a timer keeps
    # it ticking inside Qt's event loop.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(500)

    window.show()
    code = app.exec()
    lock.unlock()  # the relaunched app must be able to take the lock
    if window.restart_command:
        updater.relaunch(window.restart_command)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="NVIDIA Broadcast Bridge: Windows client")
    parser.add_argument("--install-camera", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--uninstall-camera", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    setup_logging("windows-client")
    if args.install_camera or args.uninstall_camera:
        import camera_installer

        action = camera_installer.install if args.install_camera else camera_installer.uninstall
        return camera_installer.run_action(action)
    return run_gui(ClientSettings.load())


if __name__ == "__main__":
    sys.exit(main())
