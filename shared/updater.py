"""Check GitHub Releases for a newer version and install it. No GUI dependency.

Trust model: downloads are HTTPS-only, restricted to GitHub hosts, and checked
against SHA256SUMS.txt from the same release. That catches corruption and
tampering in transit, but the checksums come from the same place as the files,
so it cannot protect against a compromised GitHub account. Protecting against
that would need a code-signing key held outside GitHub.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from shared.config import WINDOWS
from shared.version import GITHUB_REPO, __version__

log = logging.getLogger(__name__)

API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECKSUM_ASSET = "SHA256SUMS.txt"
WINDOWS_ASSET = "BroadcastServer.exe"
WINDOWS_CLIENT_ASSET = "BroadcastClient.exe"
UBUNTU_ASSET = "nvidia-broadcast-bridge-ubuntu.tar.gz"

_TIMEOUT = 20
_CHUNK = 256 * 1024
_MAX_DOWNLOAD = 500 * 1024 * 1024
_MAX_METADATA = 1024 * 1024
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

ProgressCallback = Callable[[int, int], None]


class UpdateError(Exception):
    """An update could not be checked or installed. Always safe to retry."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    page_url: str
    assets: dict = field(default_factory=dict)  # file name -> download URL


def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    match = _VERSION.match(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


# --- network ------------------------------------------------------------------


def _require_trusted(url: str) -> None:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    trusted = (host in ("github.com", "api.github.com")
               or host.endswith(".githubusercontent.com"))
    if parts.scheme != "https" or not trusted:
        raise UpdateError(f"Refusing untrusted update URL: {url}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """GitHub redirects downloads to its CDN; a redirect elsewhere is refused."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_trusted(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def _open(url: str, accept: str = "application/octet-stream"):
    _require_trusted(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": f"nvidia-broadcast-bridge/{__version__}",
        "Accept": accept,
    })
    return _opener.open(request, timeout=_TIMEOUT)


def fetch_latest() -> Optional[Release]:
    """Return the latest published release, or None if there is none."""
    try:
        with _open(API_LATEST, "application/vnd.github+json") as response:
            data = json.loads(response.read(_MAX_METADATA))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"GitHub returned HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Could not reach GitHub: {exc}") from exc

    prefix = f"https://github.com/{GITHUB_REPO}/releases/download/"
    assets = {
        asset["name"]: asset["browser_download_url"]
        for asset in data.get("assets", [])
        if str(asset.get("browser_download_url", "")).startswith(prefix)
    }
    page_url = str(data.get("html_url", ""))
    if not page_url.startswith(f"https://github.com/{GITHUB_REPO}/"):
        page_url = f"https://github.com/{GITHUB_REPO}/releases/latest"
    return Release(
        version=str(data.get("tag_name", "")).lstrip("v"),
        notes=str(data.get("body") or ""),
        page_url=page_url,
        assets=assets,
    )


def parse_checksums(text: str) -> dict[str, str]:
    """Parse sha256sum output into {file name: lowercase hash}."""
    checksums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            checksums[parts[1].lstrip("*")] = parts[0].lower()
    return checksums


def _fetch_checksums(release: Release) -> dict[str, str]:
    url = release.assets.get(CHECKSUM_ASSET)
    if not url:
        raise UpdateError(f"The release has no {CHECKSUM_ASSET}.")
    try:
        with _open(url) as response:
            return parse_checksums(response.read(_MAX_METADATA).decode("utf-8", "replace"))
    except OSError as exc:
        raise UpdateError(f"Could not download checksums: {exc}") from exc


def download_verified(url: str, destination: Path, expected_sha256: str,
                      progress: Optional[ProgressCallback] = None,
                      cancelled: Callable[[], bool] = lambda: False) -> bool:
    """Download to ``destination``. Returns False if cancelled.

    The file is removed unless it is complete and matches the hash, so nothing
    unverified is ever left in place to be installed.
    """
    digest = hashlib.sha256()
    received = 0
    complete = False
    try:
        with _open(url) as response, open(destination, "wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            while True:
                if cancelled():
                    return False
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > _MAX_DOWNLOAD:
                    raise UpdateError("The download is unexpectedly large.")
                digest.update(chunk)
                output.write(chunk)
                if progress:
                    progress(received, total)
        if digest.hexdigest() != expected_sha256.lower():
            raise UpdateError("The download failed its checksum. Nothing was installed.")
        complete = True
        return True
    except OSError as exc:
        raise UpdateError(f"Download failed: {exc}") from exc
    finally:
        if not complete:
            destination.unlink(missing_ok=True)


# --- installers ---------------------------------------------------------------


def _remove_with_retry(path: Path, attempts: int = 20) -> None:
    """Windows keeps an exe locked until the process has fully exited."""
    for _ in range(attempts):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.5)
    log.warning("Could not remove %s", path)


def _replace_with_retry(source: Path, target: Path, attempts: int = 40) -> None:
    """Move a file, waiting out brief locks.

    Antivirus scanners and the search indexer often hold a new or recently run
    exe open for a moment, which makes Windows refuse the move.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except FileNotFoundError:
            raise  # missing, not locked: waiting cannot help
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)


class WindowsInstaller:
    """Replaces the running exe.

    Windows refuses to overwrite or delete a running exe but does allow
    renaming it, so the old file is moved aside and removed by the next launch.
    """

    def __init__(self, executable: Optional[Path] = None, asset_name: str = WINDOWS_ASSET) -> None:
        self.asset_name = asset_name
        self.executable = Path(executable or sys.executable)
        self.previous = self.executable.with_name(self.executable.name + ".old")
        self.download_path = self.executable.with_name(self.executable.name + ".download")

    def supported(self) -> bool:
        return (getattr(sys, "frozen", False) and WINDOWS
                and os.access(self.executable.parent, os.W_OK))

    def install(self) -> None:
        try:
            self.previous.unlink(missing_ok=True)
            _replace_with_retry(self.executable, self.previous)
            try:
                _replace_with_retry(self.download_path, self.executable)
            except OSError:
                _replace_with_retry(self.previous, self.executable)  # put the app back
                raise
        except OSError as exc:
            _remove_with_retry(self.download_path, attempts=1)
            raise UpdateError(f"Could not replace the application: {exc}") from exc

    def restart_command(self) -> list[str]:
        return [str(self.executable)]

    def cleanup(self) -> None:
        _remove_with_retry(self.previous)
        _remove_with_retry(self.download_path, attempts=1)


class LinuxInstaller:
    """Replaces the app folder created by install_ubuntu.sh, keeping its venv.

    System parts (the driver, root helper and polkit policy) are deliberately
    not touched: a background update must not change root-owned files. A
    release that changes them must tell users to rerun the installer.
    """

    asset_name = UBUNTU_ASSET
    _MEMBERS = ("shared", "ubuntu-client")

    def __init__(self, app_root: Optional[Path] = None) -> None:
        self.app_root = Path(app_root or Path(__file__).resolve().parents[1])
        home = self.app_root.parent
        self.python = home / "venv" / "bin" / "python"
        self.staging = home / "app.new"
        self.previous = home / "app.old"
        self.download_path = home / "update.tar.gz"

    def supported(self) -> bool:
        return (self.app_root.name == "app" and self.python.exists()
                and os.access(self.app_root.parent, os.W_OK))

    def _extract(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)
        self.staging.mkdir()
        try:
            with tarfile.open(self.download_path, "r:gz") as archive:
                members = [m for m in archive.getmembers()
                           if m.name.split("/")[0] in self._MEMBERS]
                # filter="data" rejects absolute paths, ".." and special files.
                archive.extractall(self.staging, members=members, filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise UpdateError(f"Could not unpack the update: {exc}") from exc
        for required in ("ubuntu-client/main.py", "shared/version.py"):
            if not (self.staging / required).is_file():
                raise UpdateError("The update archive is incomplete.")

    def _update_dependencies(self) -> None:
        new = self.staging / "ubuntu-client" / "requirements.txt"
        old = self.app_root / "ubuntu-client" / "requirements.txt"
        # Compared by entry, so CRLF from a Windows checkout is not a change.
        if old.exists() and new.read_text().split() == old.read_text().split():
            return
        try:
            result = subprocess.run(
                [str(self.python), "-m", "pip", "install", "--quiet", "-r", str(new)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"Could not update dependencies: {exc}") from exc
        if result.returncode != 0:
            raise UpdateError(f"Could not update dependencies: {result.stderr.strip()[-300:]}")

    def install(self) -> None:
        self._extract()
        self._update_dependencies()
        try:
            shutil.rmtree(self.previous, ignore_errors=True)
            os.replace(self.app_root, self.previous)
            try:
                os.replace(self.staging, self.app_root)
            except OSError:
                os.replace(self.previous, self.app_root)  # put the app back
                raise
        except OSError as exc:
            raise UpdateError(f"Could not replace the application: {exc}") from exc
        self.download_path.unlink(missing_ok=True)

    def restart_command(self) -> list[str]:
        return [str(self.python), str(self.app_root / "ubuntu-client" / "main.py")]

    def cleanup(self) -> None:
        for path in (self.previous, self.staging, self.download_path):
            _remove_with_retry(path, attempts=1)


def current_installer():
    return WindowsInstaller() if WINDOWS else LinuxInstaller()


def download_update(release: Release, installer,
                    progress: Optional[ProgressCallback] = None,
                    cancelled: Callable[[], bool] = lambda: False) -> bool:
    """Download and verify the update. Returns False if cancelled."""
    url = release.assets.get(installer.asset_name)
    if not url:
        raise UpdateError(f"The release has no {installer.asset_name}.")
    expected = _fetch_checksums(release).get(installer.asset_name)
    if not expected:
        raise UpdateError(f"{CHECKSUM_ASSET} has no entry for {installer.asset_name}.")
    return download_verified(url, installer.download_path, expected, progress, cancelled)


def relaunch(command: list[str]) -> None:
    """Start the updated app as an independent process."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        # A one-file PyInstaller child would otherwise reuse this process's
        # temporary unpack folder, which disappears when this process exits.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        env.pop("QT_PLUGIN_PATH", None)
    kwargs: dict = {}
    if WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, **kwargs,
    )
