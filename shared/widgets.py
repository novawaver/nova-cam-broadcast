"""Qt widgets and styling shared by both GUIs."""
from __future__ import annotations

from PySide6.QtCore import QLockFile, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from shared.config import config_dir
from shared.stream_worker import StreamState

GRAY, YELLOW, GREEN, RED = "#7f8794", "#e3b341", "#3fb950", "#f85149"

STATE_COLORS = {
    StreamState.STOPPED: GRAY,
    StreamState.STARTING: YELLOW,
    StreamState.WAITING: YELLOW,
    StreamState.RECONNECTING: YELLOW,
    StreamState.CONNECTED: GREEN,
    StreamState.ERROR: RED,
}

STYLESHEET = """
QWidget { background: #14171c; color: #e6e9ef; font-size: 13px; }
QLabel#title { font-size: 16px; font-weight: 700; letter-spacing: 2px; color: #76b900; }
QLabel#cardTitle { font-size: 12px; font-weight: 700; letter-spacing: 1px; color: #9aa3b2; }
QLabel#muted { color: #9aa3b2; }
QFrame#card { background: #1b1f26; border: 1px solid #2a303a; border-radius: 8px; }
QFrame#card QLabel, QFrame#card QWidget#row { background: transparent; }
QPushButton { background: #2a303a; border: 1px solid #3a4250; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #343c49; }
QPushButton:disabled { color: #5c6573; background: #1f242c; border-color: #2a303a; }
QPushButton#primary { background: #4c7a00; border-color: #5f9600; font-weight: 700; }
QPushButton#primary:hover { background: #5a8f00; }
QPushButton#primary:disabled { background: #1f242c; border-color: #2a303a; color: #5c6573; }
QComboBox, QLineEdit, QSpinBox { background: #14171c; border: 1px solid #3a4250;
    border-radius: 4px; padding: 4px 6px; }
QPlainTextEdit { background: #0f1115; border: 1px solid #2a303a; border-radius: 6px;
    font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 11px; }
"""


def make_icon(color: str = "#76b900") -> QIcon:
    """Draw the app icon so builds need no binary assets."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(QPointF(32, 32), 28, 28)
    painter.setBrush(QColor("#14171c"))
    painter.drawEllipse(QPointF(32, 32), 12, 12)
    painter.end()
    return QIcon(pixmap)


def acquire_single_instance(name: str) -> QLockFile | None:
    """Return a held lock, or None if another instance is already running.

    The caller must keep the returned object alive for the process lifetime.
    """
    lock = QLockFile(str(config_dir() / f"{name}.lock"))
    lock.setStaleLockTime(0)  # staleness is decided by whether the PID is alive
    return lock if lock.tryLock(100) else None


class StatusDot(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_color(GRAY)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"background: {color}; border-radius: 6px;")


class StreamCard(QFrame):
    """A titled panel with detail rows, a status line and start/stop buttons."""

    start_clicked = Signal()
    stop_clicked = Signal()

    def __init__(self, title: str, start_text: str, stop_text: str, labels: dict) -> None:
        super().__init__()
        self.setObjectName("card")
        self._labels = labels

        heading = QLabel(title)
        heading.setObjectName("cardTitle")

        self.form = QFormLayout()
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.dot = StatusDot()
        self.status = QLabel(labels[StreamState.STOPPED])
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.dot)
        status_row.addWidget(self.status, 1)

        self.start_button = QPushButton(start_text)
        self.stop_button = QPushButton(stop_text)
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_clicked)
        self.stop_button.clicked.connect(self.stop_clicked)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(self.form)
        layout.addLayout(status_row)
        layout.addLayout(buttons)

    def add_row(self, label: str, widget: QWidget) -> None:
        self.form.addRow(label, widget)

    def set_state(self, state: StreamState, detail: str = "") -> None:
        text = self._labels.get(state, state.value.title())
        self.status.setText(f"{text} — {detail}" if detail else text)
        self.dot.set_color(STATE_COLORS[state])
        self.start_button.setEnabled(state.idle)
        # An Error card keeps Stop enabled so the error can be dismissed.
        self.stop_button.setEnabled(state != StreamState.STOPPED)


class LogPanel(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(1000)
        self.setMinimumHeight(110)

    def add(self, source: str, message: str) -> None:
        self.appendPlainText(f"[{source}] {message}")
