"""Install the virtual camera, and check that it can work.

The client is a single portable exe, but Windows needs the camera in a fixed
place that its Frame Server service can read. ``--install-camera`` copies the
bundled files there. It runs as a second, elevated copy of the app, so the app
itself never needs administrator rights.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from frame_writer import FILE_BYTES, data_dir, frame_path

log = logging.getLogger(__name__)

DLL = "nvbb_vcam.dll"
TOOL = "nvbb-vcam.exe"
CAMERA_LABEL = "NVIDIA Broadcast (Windows Virtual Camera)"
MINIMUM_BUILD = 22000  # MFCreateVirtualCamera arrived with Windows 11

SID_SYSTEM = "S-1-5-18"
SID_LOCAL_SERVICE = "S-1-5-19"
SID_ADMINISTRATORS = "S-1-5-32-544"
SID_USERS = "S-1-5-32-545"

_RESULT_FILE = "install-result.txt"
_TOOL_TIMEOUT = 60
_PRIVACY_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
PRIVACY_HELP = "Turn on camera access in Settings > Privacy & security > Camera."


class InstallError(Exception):
    """The camera could not be installed or removed."""


class CameraStatus(Enum):
    MISSING = "missing"
    OUTDATED = "outdated"
    READY = "ready"


def install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Broadcast Bridge"


def bundled_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "vcam"
    return Path(__file__).resolve().parent.parent / "dist" / "vcam"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def camera_status() -> CameraStatus:
    installed, bundled = install_dir() / DLL, bundled_dir() / DLL
    if not installed.is_file() or not frame_path().is_file():
        return CameraStatus.MISSING
    try:
        if frame_path().stat().st_size < FILE_BYTES:
            return CameraStatus.OUTDATED
        if bundled.is_file() and _sha256(installed) != _sha256(bundled):
            return CameraStatus.OUTDATED
    except OSError as exc:
        log.warning("Could not check the installed camera: %s", exc)
    return CameraStatus.READY


# --- checks -------------------------------------------------------------------


def _consent(root: int, subkey: str) -> Optional[str]:
    try:
        with winreg.OpenKey(root, subkey) as key:
            return str(winreg.QueryValueEx(key, "Value")[0])
    except OSError:
        return None


def camera_privacy_blocked() -> bool:
    """True if a Windows privacy switch stops desktop apps using cameras."""
    return "Deny" in (
        _consent(winreg.HKEY_LOCAL_MACHINE, _PRIVACY_KEY),
        _consent(winreg.HKEY_CURRENT_USER, _PRIVACY_KEY),
        _consent(winreg.HKEY_CURRENT_USER, _PRIVACY_KEY + r"\NonPackaged"),
    )


def windows_supported() -> bool:
    return sys.getwindowsversion().build >= MINIMUM_BUILD


# --- elevated side ------------------------------------------------------------


def acl_grants() -> list[str]:
    """Who may use the frame folder.

    The Frame Server only reads. Users can write so the client doesn't need
    administrator rights, which also lets any local user read the camera feed.
    """
    return [
        f"*{SID_SYSTEM}:(OI)(CI)F",
        f"*{SID_ADMINISTRATORS}:(OI)(CI)F",
        f"*{SID_LOCAL_SERVICE}:(OI)(CI)RX",
        f"*{SID_USERS}:(OI)(CI)M",
    ]


def _run(command: list, what: str) -> None:
    try:
        result = subprocess.run(
            [str(part) for part in command], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, errors="replace", timeout=_TOOL_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"{what}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "0x80070005" in detail:
            detail = (f"Windows denied access to the camera. Check camera access in "
                      f"Settings > Privacy & security > Camera, then click Reinstall "
                      f"Camera. ({detail})")
        raise InstallError(f"{what}: {detail or f'exit code {result.returncode}'}")


def _replace_file(source: Path, target: Path) -> None:
    """Copy a file into place, even if the Frame Server has the old one loaded.

    A loaded DLL can't be overwritten but can be renamed, and the service keeps
    using the renamed file until it next restarts.
    """
    pending = target.with_name(target.name + ".new")
    shutil.copy2(source, pending)
    try:
        os.replace(pending, target)
    except PermissionError:
        aside = target.with_name(f"{target.name}.{int(time.time())}.old")
        os.replace(target, aside)
        os.replace(pending, target)


def _remove_old_files(folder: Path) -> None:
    for path in folder.glob("*.old"):
        try:
            path.unlink()
        except OSError:
            pass  # still loaded; a later install removes it


def _prepare_frame_file() -> None:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _run(["icacls", folder, "/inheritance:r", "/grant:r", *acl_grants()], "Setting folder access")
    path = frame_path()
    with open(path, "ab") as file:
        file.truncate(FILE_BYTES)
    # A file from an earlier install keeps its old permissions otherwise.
    _run(["icacls", path, "/reset"], "Setting file access")


def install() -> None:
    """Runs elevated. Safe to repeat, which is how the camera is updated."""
    folder = install_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _remove_old_files(folder)
    for name in (DLL, TOOL):
        source = bundled_dir() / name
        if not source.is_file():
            raise InstallError(f"{name} is missing from this build.")
        _replace_file(source, folder / name)
    _prepare_frame_file()
    _run([folder / TOOL, "install"], "Registering the camera")


def _remove_on_reboot(path: Path) -> None:
    MOVEFILE_DELAY_UNTIL_REBOOT = 4
    ctypes.windll.kernel32.MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT)


def uninstall() -> None:
    """Runs elevated. The Frame Server may hold the DLL until the next restart."""
    folder = install_dir()
    if (folder / TOOL).is_file():
        _run([folder / TOOL, "uninstall"], "Removing the camera")
    shutil.rmtree(data_dir(), ignore_errors=True)
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        for path in folder.iterdir():
            _remove_on_reboot(path)
        _remove_on_reboot(folder)


def run_action(action) -> int:
    """Entry point of the elevated copy, which has no window to report errors in."""
    try:
        action()
    except (InstallError, OSError) as exc:
        log.error("Camera setup failed: %s", exc)
        try:
            data_dir().mkdir(parents=True, exist_ok=True)
            (data_dir() / _RESULT_FILE).write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        return 1
    return 0


# --- unelevated side ----------------------------------------------------------


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


@dataclass
class ElevatedResult:
    ok: bool
    message: str = ""


def _read_result() -> str:
    path = data_dir() / _RESULT_FILE
    try:
        text = path.read_text(encoding="utf-8")
        path.unlink(missing_ok=True)
        return text
    except OSError:
        return ""


def run_elevated(flag: str) -> ElevatedResult:
    """Run this app with ``flag`` through a UAC prompt and wait for it."""
    SEE_MASK_NOCLOSEPROCESS = 0x40
    SW_HIDE = 0
    ERROR_CANCELLED = 1223
    INFINITE = 0xFFFFFFFF

    parameters = flag
    if not getattr(sys, "frozen", False):
        parameters = f'"{Path(__file__).with_name("main.py")}" {flag}'
    info = _ShellExecuteInfo(
        cbSize=ctypes.sizeof(_ShellExecuteInfo), fMask=SEE_MASK_NOCLOSEPROCESS,
        lpVerb="runas", lpFile=sys.executable, lpParameters=parameters, nShow=SW_HIDE,
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == ERROR_CANCELLED:
            return ElevatedResult(False, "Cancelled.")
        return ElevatedResult(False, str(ctypes.WinError(code)))
    kernel32.WaitForSingleObject(info.hProcess, INFINITE)
    exit_code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
    kernel32.CloseHandle(info.hProcess)
    if exit_code.value == 0:
        return ElevatedResult(True)
    return ElevatedResult(False, _read_result() or f"Exit code {exit_code.value}.")
