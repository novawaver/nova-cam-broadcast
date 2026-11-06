"""The Qt side of updating: background checks, the prompt and the progress dialog."""
from __future__ import annotations

import html
import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from shared import updater
from shared.version import __version__

log = logging.getLogger(__name__)

STARTUP_DELAY_MS = 3000
MAX_NOTES = 1500


class UpdateController(QObject):
    """Owns the update flow for one window.

    ``on_installed`` receives the restart command once the new version is in
    place. It should shut the app down; main() then relaunches it.
    """

    _checked = Signal(object, str, bool)  # release, error, manual
    _progress = Signal(int, int)
    _finished = Signal(bool, str)  # installed, error

    def __init__(self, window: QWidget, settings, on_installed: Callable[[list], None],
                 installer=None) -> None:
        super().__init__(window)
        self.window = window
        self.settings = settings
        self.on_installed = on_installed
        self.installer = installer or updater.current_installer()
        self._busy = False
        self._cancel = threading.Event()
        self._dialog: QProgressDialog | None = None
        self._checked.connect(self._on_checked)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)

    def start(self) -> None:
        """Clean up after a previous update, then check if enabled."""
        threading.Thread(target=self.installer.cleanup, daemon=True).start()
        if self.settings.check_for_updates:
            QTimer.singleShot(STARTUP_DELAY_MS, lambda: self.check(manual=False))

    def check(self, manual: bool = True) -> None:
        if self._busy:
            return
        self._busy = True

        def work() -> None:
            try:
                self._checked.emit(updater.fetch_latest(), "", manual)
            except updater.UpdateError as exc:
                self._checked.emit(None, str(exc), manual)

        threading.Thread(target=work, daemon=True).start()

    def _on_checked(self, release, error: str, manual: bool) -> None:
        self._busy = False
        if error:
            log.warning("Update check failed: %s", error)
            if manual:
                QMessageBox.warning(self.window, "Updates", f"Could not check for updates.\n\n{error}")
            return
        if release is None or not updater.is_newer(release.version):
            if manual:
                QMessageBox.information(
                    self.window, "Updates", f"You have the latest version ({__version__})."
                )
            return
        if not manual and release.version == self.settings.skipped_version:
            return
        self._prompt(release, manual)

    def _prompt(self, release, manual: bool) -> None:
        can_install = self.installer.supported() and self.installer.asset_name in release.assets
        box = QMessageBox(self.window)
        box.setWindowTitle("Update Available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Version {release.version} is available. You have {__version__}.")
        notes = release.notes.strip()
        info = notes[:MAX_NOTES] + ("…" if len(notes) > MAX_NOTES else "")
        if can_install:
            info = f"{info}\n\nInstalling stops any active streams and restarts the app.".strip()
        # Escaped because Qt auto-detects HTML here: setTextFormat only affects
        # the main label, and release notes are remote content.
        box.setInformativeText(f'<div style="white-space: pre-wrap">{html.escape(info)}</div>')

        action_text = "Update Now" if can_install else "Open Release Page"
        action = box.addButton(action_text, QMessageBox.ButtonRole.AcceptRole)
        skip = box.addButton("Skip This Version", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked == skip:
            self.settings.skipped_version = release.version
            self.settings.save()
        elif clicked == action:
            if can_install:
                self._install(release)
            else:
                QDesktopServices.openUrl(QUrl(release.page_url))

    def _install(self, release) -> None:
        self._busy = True
        self._cancel.clear()
        dialog = QProgressDialog("Downloading update…", "Cancel", 0, 0, self.window)
        dialog.setWindowTitle("Updating")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.canceled.connect(self._cancel.set)
        dialog.show()
        self._dialog = dialog

        def work() -> None:
            try:
                downloaded = updater.download_update(
                    release, self.installer, self._progress.emit, self._cancel.is_set
                )
                if downloaded:
                    self.installer.install()
                self._finished.emit(downloaded, "")
            except updater.UpdateError as exc:
                self._finished.emit(False, str(exc))
            except Exception as exc:  # the dialog must never be left hanging
                log.exception("Update failed")
                self._finished.emit(False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, received: int, total: int) -> None:
        if self._dialog is None:
            return
        # Qt's progress range is int; megabytes also keep large files in range.
        self._dialog.setMaximum(total // 1_000_000 or 0)
        self._dialog.setValue(received // 1_000_000)

    def _on_finished(self, installed: bool, error: str) -> None:
        self._busy = False
        if self._dialog is not None:
            self._dialog.canceled.disconnect()
            self._dialog.close()
            self._dialog = None
        if error:
            QMessageBox.warning(self.window, "Update Failed", f"{error}\n\nYour current version still works.")
        elif installed:
            self.on_installed(self.installer.restart_command())
