"""Windows Broadcast Server desktop interface."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget,
)

import device_manager
import settings as settings_module
from shared.config import APP_NAME, AUDIO_SAMPLE_RATE
from shared.process import find_ffmpeg
from shared.stream_worker import StreamState
from shared.update_ui import UpdateController
from shared.version import __version__
from shared.widgets import STYLESHEET, LogPanel, StreamCard, make_icon
from stream_manager import CAMERA, MICROPHONE, StreamManager

LABELS = {
    StreamState.STOPPED: "Stopped",
    StreamState.STARTING: "Starting",
    StreamState.WAITING: "Listening, waiting for Ubuntu",
    StreamState.RECONNECTING: "Restarting",
    StreamState.CONNECTED: "Ubuntu connected",
    StreamState.ERROR: "Error",
}


class Bridge(QObject):
    """Carries worker-thread events onto the GUI thread via queued signals."""

    state_changed = Signal(str, object, str)
    log_line = Signal(str, str)
    devices_ready = Signal(object, str)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings

        self.width_box = self._spin(320, 3840, settings.width)
        self.height_box = self._spin(240, 2160, settings.height)
        self.fps_box = QComboBox()
        self.fps_box.addItems(["15", "24", "25", "30", "50", "60"])
        self.fps_box.setCurrentText(str(settings.fps))
        self.quality_box = self._spin(2, 31, settings.video_quality)
        self.quality_box.setToolTip("MPEG-2 quantizer: lower is better quality and more bandwidth")
        self.camera_port = self._spin(1, 65535, settings.camera_port)
        self.mic_port = self._spin(1, 65535, settings.mic_port)
        self.bind_address = QLineEdit(settings.bind_address)
        self.ffmpeg_path = QLineEdit(settings.ffmpeg_path)
        self.ffmpeg_path.setPlaceholderText("Automatic")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_path, 1)
        ffmpeg_row.addWidget(browse)
        self.tray = QCheckBox("Minimize to system tray on close")
        self.tray.setChecked(settings.minimize_to_tray)
        self.startup = QCheckBox("Launch when I sign in to Windows")
        self.startup.setChecked(settings_module.launch_at_startup_enabled())
        self.updates = QCheckBox("Check for updates on launch")
        self.updates.setChecked(settings.check_for_updates)

        form = QFormLayout()
        form.addRow("Width", self.width_box)
        form.addRow("Height", self.height_box)
        form.addRow("FPS", self.fps_box)
        form.addRow("Video quality", self.quality_box)
        form.addRow("Camera port", self.camera_port)
        form.addRow("Microphone port", self.mic_port)
        form.addRow("Bind address", self.bind_address)
        form.addRow("FFmpeg", ffmpeg_row)
        form.addRow(self.tray)
        form.addRow(self.startup)
        form.addRow(self.updates)

        note = QLabel("Changes apply the next time a stream starts.")
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

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate ffmpeg.exe", "", "ffmpeg.exe (ffmpeg.exe)")
        if path:
            self.ffmpeg_path.setText(path)

    def _save(self) -> None:
        if self.camera_port.value() == self.mic_port.value():
            QMessageBox.warning(self, "Settings", "The two streams need different ports.")
            return
        s = self.settings
        s.width = self.width_box.value()
        s.height = self.height_box.value()
        s.fps = int(self.fps_box.currentText())
        s.video_quality = self.quality_box.value()
        s.camera_port = self.camera_port.value()
        s.mic_port = self.mic_port.value()
        s.bind_address = self.bind_address.text().strip() or "0.0.0.0"
        s.ffmpeg_path = self.ffmpeg_path.text().strip()
        s.minimize_to_tray = self.tray.isChecked()
        s.check_for_updates = self.updates.isChecked()
        try:
            settings_module.set_launch_at_startup(self.startup.isChecked())
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not change launch at startup: {exc}")
        s.save()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, settings) -> None:
        super().__init__()
        self.settings = settings
        self._quitting = False
        self.restart_command = None  # set when an update needs a relaunch
        self._states = {CAMERA: StreamState.STOPPED, MICROPHONE: StreamState.STOPPED}

        self.bridge = Bridge()
        self.bridge.state_changed.connect(self._on_state)
        self.bridge.log_line.connect(self._on_log)
        self.bridge.devices_ready.connect(self._on_devices)
        self.manager = StreamManager(
            settings, self.bridge.state_changed.emit, self.bridge.log_line.emit
        )

        self.setWindowTitle(f"{APP_NAME} {__version__} — Windows Server")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(STYLESHEET)
        self.resize(500, 720)

        title = QLabel("NVIDIA BROADCAST BRIDGE")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.camera_combo = QComboBox()
        self.camera_info = QLabel()
        self.camera_card = StreamCard("CAMERA", "Start Camera", "Stop Camera", LABELS)
        self.camera_card.add_row("Device", self.camera_combo)
        self.camera_card.add_row("Format", self.camera_info)
        self.camera_card.start_clicked.connect(lambda: self._start(self.manager.camera))
        self.camera_card.stop_clicked.connect(self.manager.camera.stop)

        self.mic_combo = QComboBox()
        self.mic_info = QLabel()
        self.mic_card = StreamCard("MICROPHONE", "Start Mic", "Stop Mic", LABELS)
        self.mic_card.add_row("Device", self.mic_combo)
        self.mic_card.add_row("Format", self.mic_info)
        self.mic_card.start_clicked.connect(lambda: self._start(self.manager.microphone))
        self.mic_card.stop_clicked.connect(self.manager.microphone.stop)

        start_both = QPushButton("START BOTH")
        start_both.setObjectName("primary")
        start_both.clicked.connect(self._start_both)
        stop_both = QPushButton("STOP BOTH")
        stop_both.clicked.connect(self.manager.stop_all)
        refresh = QPushButton("Refresh Devices")
        refresh.clicked.connect(self._detect_devices)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self._open_settings)

        both_row = QHBoxLayout()
        both_row.addWidget(start_both)
        both_row.addWidget(stop_both)
        tools_row = QHBoxLayout()
        tools_row.addWidget(refresh)
        tools_row.addWidget(settings_button)
        updates_button = QPushButton("Check for Updates")
        tools_row.addWidget(updates_button)

        self.connection_label = QLabel()
        self.logs = LogPanel()

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(self.camera_card)
        layout.addWidget(self.mic_card)
        layout.addLayout(both_row)
        layout.addLayout(tools_row)
        layout.addWidget(self.connection_label)
        layout.addWidget(self.logs, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

        self.updates = UpdateController(self, settings, self._restart_for_update)
        updates_button.clicked.connect(lambda: self.updates.check(manual=True))

        self._create_tray()
        self._refresh_info()
        self._update_connection()
        self._detect_devices()
        self.updates.start()

    # --- tray -----------------------------------------------------------------

    def _create_tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = make_icon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Show").triggered.connect(self._show_window)
        menu.addAction("Start Both").triggered.connect(self._start_both)
        menu.addAction("Stop Both").triggered.connect(self.manager.stop_all)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu  # the tray does not take ownership
        self.tray.activated.connect(
            lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and self._show_window()
        )
        self.tray.show()

    def _show_window(self) -> None:
        self.showNormal()
        self.activateWindow()

    def _restart_for_update(self, command: list) -> None:
        self.restart_command = command
        self._quit()

    def _quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event) -> None:
        if self.settings.minimize_to_tray and self.tray is not None and not self._quitting:
            self.hide()
            event.ignore()
            return
        self.shutdown()
        event.accept()
        QApplication.quit()

    def shutdown(self) -> None:
        self._store_devices()
        self.settings.save()
        self.manager.shutdown()
        if self.tray is not None:
            self.tray.hide()

    # --- devices --------------------------------------------------------------

    def _detect_devices(self) -> None:
        self._store_devices()  # a refresh must not discard the current choice
        path = find_ffmpeg(self.settings.ffmpeg_path)
        if not path:
            self._on_log("App", "FFmpeg not found. Install it or set its path in Settings.")
            return

        def work() -> None:
            try:
                self.bridge.devices_ready.emit(device_manager.list_devices(path), "")
            except Exception as exc:
                self.bridge.devices_ready.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_devices(self, devices, error: str) -> None:
        if devices is None:
            self._on_log("App", f"Device detection failed: {error}")
            return
        self._fill(self.camera_combo, devices.video, self.settings.camera_device)
        self._fill(self.mic_combo, devices.audio, self.settings.microphone_device)
        self._on_log("App", f"Found {len(devices.video)} camera(s), {len(devices.audio)} microphone(s)")
        for name, found in ((self.settings.camera_device, devices.video),
                            (self.settings.microphone_device, devices.audio)):
            if name not in found:
                self._on_log("App", f"Not detected: {name}. Is NVIDIA Broadcast running?")

    @staticmethod
    def _fill(combo: QComboBox, names: list[str], selected: str) -> None:
        combo.clear()
        combo.addItems(names)
        if selected not in names:
            combo.addItem(selected)  # keep the choice so it works when it returns
        combo.setCurrentText(selected)

    def _store_devices(self) -> None:
        if self.camera_combo.currentText():
            self.settings.camera_device = self.camera_combo.currentText()
        if self.mic_combo.currentText():
            self.settings.microphone_device = self.mic_combo.currentText()

    # --- actions --------------------------------------------------------------

    def _start(self, stream) -> None:
        self._store_devices()
        self.settings.save()
        stream.start()

    def _start_both(self) -> None:
        self._store_devices()
        self.settings.save()
        self.manager.start_all()

    def _open_settings(self) -> None:
        self._store_devices()
        if SettingsDialog(self.settings, self).exec():
            self._refresh_info()

    def _refresh_info(self) -> None:
        s = self.settings
        self.camera_info.setText(f"{s.width} × {s.height} @ {s.fps} FPS, port {s.camera_port}")
        self.mic_info.setText(f"{AUDIO_SAMPLE_RATE} Hz mono, port {s.mic_port}")

    # --- worker events --------------------------------------------------------

    def _on_state(self, name: str, state: StreamState, detail: str) -> None:
        self._states[name] = state
        card = self.camera_card if name == CAMERA else self.mic_card
        card.set_state(state, detail)
        if state == StreamState.ERROR:
            self._on_log(name, detail)
        self._update_connection()

    def _on_log(self, source: str, message: str) -> None:
        self.logs.add(source, message)

    def _update_connection(self) -> None:
        def word(name: str) -> str:
            return "connected" if self._states[name] == StreamState.CONNECTED else "not connected"

        self.connection_label.setText(
            f"Windows ↔ Ubuntu: camera {word(CAMERA)}, microphone {word(MICROPHONE)}"
        )
