"""Ubuntu Broadcast Client desktop interface."""
from __future__ import annotations

import html
import re
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from connection_manager import CAMERA, MICROPHONE, ConnectionManager, run_diagnostics
from shared.config import APP_NAME, AUDIO_SAMPLE_RATE
from shared.stream_worker import StreamState
from shared.widgets import GREEN, RED, STYLESHEET, LogPanel, StreamCard, make_icon

LABELS = {
    StreamState.STOPPED: "Disconnected",
    StreamState.STARTING: "Connecting",
    StreamState.WAITING: "Waiting for stream",
    StreamState.RECONNECTING: "Reconnecting",
    StreamState.CONNECTED: "Connected",
    StreamState.ERROR: "Error",
}

_VALID_HOST = re.compile(r"^[A-Za-z0-9.-]+$")


class Bridge(QObject):
    """Carries worker-thread events onto the GUI thread via queued signals."""

    state_changed = Signal(str, object, str)
    log_line = Signal(str, str)
    diagnostics_ready = Signal(object, object)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings

        self.host = QLineEdit(settings.host)
        self.camera_port = self._spin(1, 65535, settings.camera_port)
        self.mic_port = self._spin(1, 65535, settings.mic_port)
        self.width_box = self._spin(320, 3840, settings.width)
        self.height_box = self._spin(240, 2160, settings.height)
        self.fps_box = self._spin(1, 60, settings.fps)
        self.video_device = QLineEdit(settings.video_device)
        self.ffmpeg_path = QLineEdit(settings.ffmpeg_path)
        self.ffmpeg_path.setPlaceholderText("Automatic")

        form = QFormLayout()
        form.addRow("Windows host", self.host)
        form.addRow("Camera port", self.camera_port)
        form.addRow("Microphone port", self.mic_port)
        form.addRow("Output width", self.width_box)
        form.addRow("Output height", self.height_box)
        form.addRow("Output FPS", self.fps_box)
        form.addRow("Video device", self.video_device)
        form.addRow("FFmpeg", self.ffmpeg_path)

        note = QLabel("Changes apply the next time a stream connects.")
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

    @staticmethod
    def _spin(low: int, high: int, value: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setValue(value)
        return box

    def _save(self) -> None:
        host = self.host.text().strip()
        if not _VALID_HOST.match(host):
            QMessageBox.warning(self, "Settings", "Enter a valid host name or IPv4 address.")
            return
        if self.camera_port.value() == self.mic_port.value():
            QMessageBox.warning(self, "Settings", "The two streams need different ports.")
            return
        s = self.settings
        s.host = host
        s.camera_port = self.camera_port.value()
        s.mic_port = self.mic_port.value()
        s.width = self.width_box.value()
        s.height = self.height_box.value()
        s.fps = self.fps_box.value()
        s.video_device = self.video_device.text().strip() or "/dev/video10"
        s.ffmpeg_path = self.ffmpeg_path.text().strip()
        s.save()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, settings) -> None:
        super().__init__()
        self.settings = settings
        self._states = {CAMERA: StreamState.STOPPED, MICROPHONE: StreamState.STOPPED}
        self._shut_down = False

        self.bridge = Bridge()
        self.bridge.state_changed.connect(self._on_state)
        self.bridge.log_line.connect(self._on_log)
        self.bridge.diagnostics_ready.connect(self._on_diagnostics)
        self.manager = ConnectionManager(
            settings, self.bridge.state_changed.emit, self.bridge.log_line.emit
        )

        self.setWindowTitle(f"{APP_NAME} — Ubuntu Client")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(STYLESHEET)
        self.resize(500, 780)

        title = QLabel("NVIDIA BROADCAST BRIDGE")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.camera_source = QLabel()
        self.camera_format = QLabel()
        self.camera_card = StreamCard("CAMERA", "Connect Camera", "Disconnect Camera", LABELS)
        self.camera_card.add_row("Device", self.camera_source)
        self.camera_card.add_row("Format", self.camera_format)
        self.camera_card.start_clicked.connect(self.manager.camera.start)
        self.camera_card.stop_clicked.connect(self.manager.camera.stop)

        self.mic_source = QLabel()
        self.mic_format = QLabel()
        self.mic_card = StreamCard("MICROPHONE", "Connect Microphone", "Disconnect Microphone", LABELS)
        self.mic_card.add_row("Device", self.mic_source)
        self.mic_card.add_row("Format", self.mic_format)
        self.mic_card.start_clicked.connect(self.manager.microphone.start)
        self.mic_card.stop_clicked.connect(self.manager.microphone.stop)

        connect_both = QPushButton("CONNECT BOTH")
        connect_both.setObjectName("primary")
        connect_both.clicked.connect(self.manager.start_all)
        disconnect_both = QPushButton("DISCONNECT BOTH")
        disconnect_both.clicked.connect(self.manager.stop_all)
        both_row = QHBoxLayout()
        both_row.addWidget(connect_both)
        both_row.addWidget(disconnect_both)

        self.refresh_button = QPushButton("Refresh Checks")
        self.refresh_button.clicked.connect(self._run_checks)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self._open_settings)
        tools_row = QHBoxLayout()
        tools_row.addWidget(self.refresh_button)
        tools_row.addWidget(settings_button)

        self.checks_label = QLabel("Checking system…")
        self.checks_label.setWordWrap(True)
        self.checks_label.setTextFormat(Qt.TextFormat.RichText)
        self.connection_label = QLabel()
        self.logs = LogPanel()

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(self.camera_card)
        layout.addWidget(self.mic_card)
        layout.addLayout(both_row)
        layout.addLayout(tools_row)
        layout.addWidget(self.checks_label)
        layout.addWidget(self.connection_label)
        layout.addWidget(self.logs, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

        self._refresh_info()
        self._update_connection()
        self._run_checks()

    # --- lifecycle ------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self.settings.save()
        self.manager.shutdown()

    # --- actions --------------------------------------------------------------

    def _open_settings(self) -> None:
        if SettingsDialog(self.settings, self).exec():
            self._refresh_info()
            self._run_checks()

    def _refresh_info(self) -> None:
        s = self.settings
        self.camera_source.setText(f"{s.video_device} ← {s.host}:{s.camera_port}")
        self.camera_format.setText(f"{s.width} × {s.height} @ {s.fps} FPS")
        self.mic_source.setText(f"{s.microphone_name} ← {s.host}:{s.mic_port}")
        self.mic_format.setText(f"{AUDIO_SAMPLE_RATE} Hz mono")

    def _run_checks(self) -> None:
        self.refresh_button.setEnabled(False)

        def work() -> None:
            try:
                checks, inputs = run_diagnostics(self.settings)
                self.bridge.diagnostics_ready.emit(checks, inputs)
            except Exception as exc:
                self.bridge.diagnostics_ready.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_diagnostics(self, checks, inputs) -> None:
        self.refresh_button.setEnabled(True)
        if checks is None:
            self.checks_label.setText(f"Checks failed: {html.escape(inputs)}")
            return
        rows = [
            f'<span style="color:{GREEN if c.ok else RED}">●</span> '
            f"{html.escape(c.name)}: {html.escape(c.detail)}"
            for c in checks
        ]
        if inputs:
            rows.append("<b>Input devices:</b> " + html.escape(", ".join(inputs)))
        self.checks_label.setText("<br>".join(rows))

    # --- worker events --------------------------------------------------------

    def _on_state(self, name: str, state: StreamState, detail: str) -> None:
        changed = self._states[name] != state
        self._states[name] = state
        card = self.camera_card if name == CAMERA else self.mic_card
        card.set_state(state, detail)
        if state == StreamState.ERROR:
            self._on_log(name, detail)
        self._update_connection()
        # Detail-only updates such as FPS jitter must not spawn diagnostics.
        if changed and state in (StreamState.CONNECTED, StreamState.ERROR):
            if self.refresh_button.isEnabled():
                self._run_checks()

    def _on_log(self, source: str, message: str) -> None:
        self.logs.add(source, message)

    def _update_connection(self) -> None:
        def word(name: str) -> str:
            return "connected" if self._states[name] == StreamState.CONNECTED else "not connected"

        self.connection_label.setText(
            f"Windows ↔ Ubuntu: camera {word(CAMERA)}, microphone {word(MICROPHONE)}"
        )
