"""Windows Broadcast Client desktop interface."""
from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

import camera_installer
from camera_installer import CAMERA_LABEL, PRIVACY_HELP, CameraStatus
from camera_receiver import CameraReceiver
from frame_writer import FPS, HEIGHT, WIDTH
from shared import updater
from shared.config import APP_NAME
from shared.process import find_ffmpeg
from shared.stream_worker import StreamState
from shared.update_ui import UpdateController
from shared.version import __version__
from shared.widgets import GREEN, RED, STYLESHEET, LogPanel, StreamCard, make_icon

LABELS = {
    StreamState.STOPPED: "Disconnected",
    StreamState.STARTING: "Connecting",
    StreamState.WAITING: "Waiting for stream",
    StreamState.RECONNECTING: "Reconnecting",
    StreamState.CONNECTED: "Connected",
    StreamState.ERROR: "Error",
}

MICROPHONE_NOTE = (
    "Microphone: this client carries video only. Turn on Audio Input in the VM's "
    "VirtualBox audio settings to use the host's microphone."
)

_VALID_HOST = re.compile(r"^[A-Za-z0-9.-]+$")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(settings) -> tuple[list[Check], CameraStatus]:
    """May block; call off the GUI thread."""
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    status = camera_installer.camera_status()
    camera_detail = {
        CameraStatus.READY: "installed",
        CameraStatus.OUTDATED: "needs updating; click Update Camera",
        CameraStatus.MISSING: "not installed; click Install Camera",
    }[status]
    blocked = camera_installer.camera_privacy_blocked()
    supported = camera_installer.windows_supported()
    return [
        Check("FFmpeg", bool(ffmpeg), ffmpeg or "not found"),
        Check("Windows", supported, "supported" if supported else "Windows 11 is required"),
        Check("Virtual camera", status == CameraStatus.READY, camera_detail),
        Check("Camera privacy", not blocked, PRIVACY_HELP if blocked else "camera access is on"),
    ], status


class Bridge(QObject):
    """Carries worker-thread events onto the GUI thread via queued signals."""

    state_changed = Signal(str, object, str)
    log_line = Signal(str, str)
    checks_ready = Signal(object, object)
    setup_finished = Signal(object)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings

        self.host = QLineEdit(settings.host)
        self.camera_port = QSpinBox()
        self.camera_port.setRange(1, 65535)
        self.camera_port.setValue(settings.camera_port)
        self.ffmpeg_path = QLineEdit(settings.ffmpeg_path)
        self.ffmpeg_path.setPlaceholderText("Automatic")
        self.updates = QCheckBox("Check for updates on launch")
        self.updates.setChecked(settings.check_for_updates)

        form = QFormLayout()
        form.addRow("Windows host", self.host)
        form.addRow("Camera port", self.camera_port)
        form.addRow("FFmpeg", self.ffmpeg_path)
        form.addRow(self.updates)

        note = QLabel("Changes apply the next time the camera connects.")
        note.setObjectName("muted")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _save(self) -> None:
        host = self.host.text().strip()
        if not _VALID_HOST.match(host):
            QMessageBox.warning(self, "Settings", "Enter a valid host name or IPv4 address.")
            return
        s = self.settings
        s.host = host
        s.camera_port = self.camera_port.value()
        s.ffmpeg_path = self.ffmpeg_path.text().strip()
        s.check_for_updates = self.updates.isChecked()
        s.save()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, settings) -> None:
        super().__init__()
        self.settings = settings
        self._shut_down = False
        self._setup_running = False
        self.restart_command = None  # set when an update needs a relaunch

        self.bridge = Bridge()
        self.bridge.state_changed.connect(self._on_state)
        self.bridge.log_line.connect(self._on_log)
        self.bridge.checks_ready.connect(self._on_checks)
        self.bridge.setup_finished.connect(self._on_setup_finished)
        self.receiver = CameraReceiver(
            settings, self.bridge.state_changed.emit, self.bridge.log_line.emit
        )

        self.setWindowTitle(f"{APP_NAME} {__version__} — Windows Client")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(STYLESHEET)
        self.resize(500, 640)

        title = QLabel("NVIDIA BROADCAST BRIDGE")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.source_label = QLabel()
        self.card = StreamCard("CAMERA", "Connect Camera", "Disconnect Camera", LABELS)
        self.card.add_row("Device", QLabel(CAMERA_LABEL))
        self.card.add_row("Source", self.source_label)
        self.card.add_row("Format", QLabel(f"{WIDTH} × {HEIGHT} @ {FPS} FPS"))
        self.card.start_clicked.connect(self.receiver.start)
        self.card.stop_clicked.connect(self.receiver.stop)

        self.install_button = QPushButton("Install Camera")
        self.install_button.setObjectName("primary")
        self.install_button.clicked.connect(self._install_camera)
        self.remove_button = QPushButton("Remove Camera")
        self.remove_button.clicked.connect(self._remove_camera)
        setup_row = QHBoxLayout()
        setup_row.addWidget(self.install_button)
        setup_row.addWidget(self.remove_button)

        self.refresh_button = QPushButton("Refresh Checks")
        self.refresh_button.clicked.connect(self._run_checks)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self._open_settings)
        updates_button = QPushButton("Check for Updates")
        tools_row = QHBoxLayout()
        tools_row.addWidget(self.refresh_button)
        tools_row.addWidget(settings_button)
        tools_row.addWidget(updates_button)

        self.checks_label = QLabel("Checking system…")
        self.checks_label.setWordWrap(True)
        self.checks_label.setTextFormat(Qt.TextFormat.RichText)
        microphone = QLabel(MICROPHONE_NOTE)
        microphone.setObjectName("muted")
        microphone.setWordWrap(True)
        self.logs = LogPanel()

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(self.card)
        layout.addLayout(setup_row)
        layout.addLayout(tools_row)
        layout.addWidget(self.checks_label)
        layout.addWidget(microphone)
        layout.addWidget(self.logs, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

        self.updates = UpdateController(
            self, settings, self._restart_for_update,
            installer=updater.WindowsInstaller(asset_name=updater.WINDOWS_CLIENT_ASSET),
        )
        updates_button.clicked.connect(lambda: self.updates.check(manual=True))

        self._refresh_info()
        self._run_checks()
        self.updates.start()

    # --- lifecycle ------------------------------------------------------------

    def _restart_for_update(self, command: list) -> None:
        self.restart_command = command
        self.close()

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self.settings.save()
        self.receiver.stop(wait=True)

    # --- actions --------------------------------------------------------------

    def _open_settings(self) -> None:
        if SettingsDialog(self.settings, self).exec():
            self._refresh_info()
            self._run_checks()

    def _refresh_info(self) -> None:
        self.source_label.setText(f"{self.settings.host}:{self.settings.camera_port}")

    def _run_checks(self) -> None:
        self.refresh_button.setEnabled(False)

        def work() -> None:
            try:
                self.bridge.checks_ready.emit(*run_checks(self.settings))
            except Exception as exc:
                self.bridge.checks_ready.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_checks(self, checks, status) -> None:
        self.refresh_button.setEnabled(True)
        if checks is None:
            self.checks_label.setText(f"Checks failed: {html.escape(status)}")
            return
        self.checks_label.setText("<br>".join(
            f'<span style="color:{GREEN if c.ok else RED}">●</span> '
            f"{html.escape(c.name)}: {html.escape(c.detail)}"
            for c in checks
        ))
        self.install_button.setText({
            CameraStatus.MISSING: "Install Camera",
            CameraStatus.OUTDATED: "Update Camera",
            # Files can be in place while Windows refused the registration, so
            # installing again must stay possible.
            CameraStatus.READY: "Reinstall Camera",
        }[status])
        if not self._setup_running:
            self.install_button.setEnabled(True)
            self.remove_button.setEnabled(status != CameraStatus.MISSING)

    def _install_camera(self) -> None:
        self._run_setup("--install-camera", "Installing the camera")

    def _remove_camera(self) -> None:
        answer = QMessageBox.question(
            self, "Remove Camera", "Remove the virtual camera from Windows?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run_setup("--uninstall-camera", "Removing the camera")

    def _run_setup(self, flag: str, description: str) -> None:
        # The frame file is replaced during setup, so it can't stay in use.
        self.receiver.stop()
        self._setup_running = True
        self.install_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self._on_log("Setup", f"{description}. Approve the Windows prompt.")

        def work() -> None:
            self.bridge.setup_finished.emit(camera_installer.run_elevated(flag))

        threading.Thread(target=work, daemon=True).start()

    def _on_setup_finished(self, result) -> None:
        self._setup_running = False
        self._on_log("Setup", "Done." if result.ok else result.message)
        if not result.ok and result.message != "Cancelled.":
            QMessageBox.warning(self, "Camera Setup", result.message)
        self._run_checks()

    # --- worker events --------------------------------------------------------

    def _on_state(self, name: str, state: StreamState, detail: str) -> None:
        self.card.set_state(state, detail)
        if state == StreamState.ERROR:
            self._on_log(name, detail)

    def _on_log(self, source: str, message: str) -> None:
        self.logs.add(source, message)
