"""Tests for the updater. No network is used."""
import hashlib
import io
import json
import tarfile
import threading
import urllib.error

import pytest

from shared import updater
from shared.updater import (
    LinuxInstaller, Release, UpdateError, WindowsInstaller, is_newer, parse_checksums,
    parse_version,
)

REPO_URL = f"https://github.com/{updater.GITHUB_REPO}"


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}


def serve(monkeypatch, routes: dict) -> None:
    def fake_open(url, accept="application/octet-stream"):
        updater._require_trusted(url)
        body = routes[url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    monkeypatch.setattr(updater, "_open", fake_open)


def test_version_ordering_is_numeric():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert is_newer("0.10.0", "0.9.9"), "must not compare as text"
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("0.0.9", "0.1.0")


@pytest.mark.parametrize("bad", ["", "latest", "1.2", "1.2.3-beta", "v1.2.3.4"])
def test_unparseable_versions_are_never_newer(bad):
    assert not is_newer(bad, "0.1.0")


@pytest.mark.parametrize("url", [
    "http://github.com/x",                     # not HTTPS
    "https://evil.example/x",
    "https://github.com.evil.example/x",       # lookalike prefix
    "https://evilgithubusercontent.com/x",     # lookalike suffix
])
def test_untrusted_urls_are_refused(url):
    with pytest.raises(UpdateError):
        updater._require_trusted(url)


@pytest.mark.parametrize("url", [
    f"{REPO_URL}/releases/download/v1.0.0/a.exe",
    "https://release-assets.githubusercontent.com/x",
    "https://objects.githubusercontent.com/x",
])
def test_github_hosts_are_trusted(url):
    updater._require_trusted(url)


def test_redirect_away_from_github_is_refused():
    handler = updater._CheckedRedirects()
    with pytest.raises(UpdateError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/a.exe")


def test_fetch_latest_parses_a_release_and_drops_foreign_assets(monkeypatch):
    payload = {
        "tag_name": "v1.4.0", "body": "Notes", "html_url": f"{REPO_URL}/releases/tag/v1.4.0",
        "assets": [
            {"name": "good.exe", "browser_download_url": f"{REPO_URL}/releases/download/v1.4.0/good.exe"},
            {"name": "bad.exe", "browser_download_url": "https://evil.example/bad.exe"},
        ],
    }
    serve(monkeypatch, {updater.API_LATEST: json.dumps(payload).encode()})
    release = updater.fetch_latest()
    assert release.version == "1.4.0"
    assert list(release.assets) == ["good.exe"]


def test_no_published_release_is_not_an_error(monkeypatch):
    error = urllib.error.HTTPError(updater.API_LATEST, 404, "Not Found", {}, None)
    serve(monkeypatch, {updater.API_LATEST: error})
    assert updater.fetch_latest() is None


def test_network_failure_is_reported_as_update_error(monkeypatch):
    serve(monkeypatch, {updater.API_LATEST: urllib.error.URLError("offline")})
    with pytest.raises(UpdateError):
        updater.fetch_latest()


def test_checksum_parsing():
    digest = "a" * 64
    assert parse_checksums(f"{digest}  one.exe\n{digest} *two.exe\ngarbage\n") == {
        "one.exe": digest, "two.exe": digest,
    }


URL = f"{REPO_URL}/releases/download/v9/app"


def test_download_keeps_a_verified_file(monkeypatch, tmp_path):
    serve(monkeypatch, {URL: b"payload"})
    target = tmp_path / "app"
    seen = []
    ok = updater.download_verified(
        URL, target, hashlib.sha256(b"payload").hexdigest(), lambda r, t: seen.append((r, t)),
    )
    assert ok and target.read_bytes() == b"payload"
    assert seen == [(7, 7)]


def test_download_removes_a_file_that_fails_its_checksum(monkeypatch, tmp_path):
    serve(monkeypatch, {URL: b"tampered"})
    target = tmp_path / "app"
    with pytest.raises(UpdateError):
        updater.download_verified(URL, target, hashlib.sha256(b"payload").hexdigest())
    assert not target.exists()


def test_cancelled_download_leaves_nothing_behind(monkeypatch, tmp_path):
    serve(monkeypatch, {URL: b"payload"})
    target = tmp_path / "app"
    assert not updater.download_verified(URL, target, "0" * 64, cancelled=lambda: True)
    assert not target.exists()


def make_release(**assets) -> Release:
    return Release("9.0.0", "", "", assets)


def test_download_update_requires_a_checksum_entry(monkeypatch, tmp_path):
    installer = WindowsInstaller(tmp_path / "App.exe")
    checksums_url = f"{REPO_URL}/sums"
    release = Release("9.0.0", "", "", {
        installer.asset_name: URL, updater.CHECKSUM_ASSET: checksums_url,
    })
    serve(monkeypatch, {checksums_url: b"", URL: b"payload"})
    with pytest.raises(UpdateError):
        updater.download_update(release, installer)
    assert not installer.download_path.exists()


def test_download_update_requires_checksums_at_all(tmp_path):
    installer = WindowsInstaller(tmp_path / "App.exe")
    with pytest.raises(UpdateError):
        updater.download_update(make_release(**{installer.asset_name: URL}), installer)


def test_windows_swap_keeps_the_old_exe_aside(tmp_path):
    exe = tmp_path / "App.exe"
    exe.write_bytes(b"old")
    installer = WindowsInstaller(exe)
    installer.download_path.write_bytes(b"new")
    installer.install()
    assert exe.read_bytes() == b"new"
    assert installer.previous.read_bytes() == b"old"
    installer.cleanup()
    assert not installer.previous.exists()


def test_windows_swap_restores_the_app_if_the_new_file_cannot_be_placed(tmp_path):
    exe = tmp_path / "App.exe"
    exe.write_bytes(b"old")
    installer = WindowsInstaller(exe)  # no download file, so the second move fails
    with pytest.raises(UpdateError):
        installer.install()
    assert exe.read_bytes() == b"old"


@pytest.mark.skipif(not updater.WINDOWS, reason="file sharing locks are Windows behaviour")
def test_windows_swap_waits_out_a_brief_file_lock(tmp_path):
    """A scanner holding the exe open must not fail the update."""
    exe = tmp_path / "App.exe"
    exe.write_bytes(b"old")
    installer = WindowsInstaller(exe)
    installer.download_path.write_bytes(b"new")
    holder = open(exe, "rb")
    threading.Timer(1.0, holder.close).start()
    installer.install()
    assert exe.read_bytes() == b"new"


def test_failed_swap_does_not_leave_the_download_behind(tmp_path, monkeypatch):
    exe = tmp_path / "App.exe"
    exe.write_bytes(b"old")
    installer = WindowsInstaller(exe)
    installer.download_path.write_bytes(b"new")

    def locked(source, target):
        raise PermissionError("locked")

    monkeypatch.setattr(updater.os, "replace", locked)
    monkeypatch.setattr(updater.time, "sleep", lambda _: None)
    with pytest.raises(UpdateError):
        installer.install()
    assert exe.read_bytes() == b"old"
    assert not installer.download_path.exists()


def linux_install(tmp_path):
    home = tmp_path / "home"
    (home / "app" / "ubuntu-client").mkdir(parents=True)
    (home / "app" / "ubuntu-client" / "requirements.txt").write_text("PySide6\n")
    (home / "app" / "ubuntu-client" / "main.py").write_text("old")
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "venv" / "bin" / "python").write_text("")
    return LinuxInstaller(home / "app")


def write_archive(path, files: dict) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


GOOD_ARCHIVE = {
    "ubuntu-client/main.py": "new",
    "ubuntu-client/requirements.txt": "PySide6\n",
    "shared/version.py": "x",
    "scripts/install_ubuntu.sh": "not part of the app folder",
}


def test_linux_support_requires_the_installed_layout(tmp_path):
    assert linux_install(tmp_path).supported()
    assert not LinuxInstaller(tmp_path / "checkout").supported()


def test_linux_swap_replaces_the_app_and_installs_only_app_folders(tmp_path):
    installer = linux_install(tmp_path)
    write_archive(installer.download_path, GOOD_ARCHIVE)
    installer.install()
    assert (installer.app_root / "ubuntu-client" / "main.py").read_text() == "new"
    assert not (installer.app_root / "scripts").exists()
    assert (installer.previous / "ubuntu-client" / "main.py").read_text() == "old"
    assert not installer.download_path.exists()
    installer.cleanup()
    assert not installer.previous.exists()


def test_linux_rejects_an_incomplete_archive_and_keeps_the_app(tmp_path):
    installer = linux_install(tmp_path)
    write_archive(installer.download_path, {"ubuntu-client/main.py": "new"})
    with pytest.raises(UpdateError):
        installer.install()
    assert (installer.app_root / "ubuntu-client" / "main.py").read_text() == "old"


def test_linux_rejects_path_traversal(tmp_path):
    installer = linux_install(tmp_path)
    write_archive(installer.download_path, {
        **GOOD_ARCHIVE, "ubuntu-client/../../escaped.txt": "x",
    })
    with pytest.raises(UpdateError):
        installer.install()
    assert not (tmp_path / "home" / "escaped.txt").exists()
    assert (installer.app_root / "ubuntu-client" / "main.py").read_text() == "old"


def test_linux_failed_dependency_install_keeps_the_app(tmp_path, monkeypatch):
    installer = linux_install(tmp_path)
    write_archive(installer.download_path, {
        **GOOD_ARCHIVE, "ubuntu-client/requirements.txt": "SomethingNew\n",
    })

    def fail(*args, **kwargs):
        raise OSError("no pip")

    monkeypatch.setattr(updater.subprocess, "run", fail)
    with pytest.raises(UpdateError):
        installer.install()
    assert (installer.app_root / "ubuntu-client" / "main.py").read_text() == "old"


def test_relaunch_clears_the_frozen_environment(monkeypatch):
    captured = {}
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: captured.update(kw))
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setenv("QT_PLUGIN_PATH", "/stale")
    updater.relaunch(["app"])
    assert captured["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "QT_PLUGIN_PATH" not in captured["env"]
