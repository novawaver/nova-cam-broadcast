"""Windows client logic that needs no camera, network or elevation."""
import io
import re
import struct
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

ROOT = Path(__file__).resolve().parent.parent
HEADER_FILE = ROOT / "windows-client" / "vcam" / "frame_file.h"


def constant(name: str) -> int:
    match = re.search(rf"constexpr \w+ {name} = (\w+);", HEADER_FILE.read_text())
    return int(match.group(1), 0)


def test_layout_matches_the_camera():
    import frame_writer as fw

    assert fw.MAGIC == constant("kFrameMagic")
    assert fw.VERSION == constant("kFrameVersion")
    assert fw.WIDTH == constant("kWidth")
    assert fw.HEIGHT == constant("kHeight")
    assert fw.FPS == constant("kFps")
    assert fw.SLOTS == constant("kSlots")
    assert fw.HEADER_BYTES == constant("kHeaderBytes")
    assert struct.calcsize("<IIIIIIQQ") == 40


def make_writer(tmp_path):
    import frame_writer as fw

    path = tmp_path / "frames"
    path.write_bytes(bytes(fw.FILE_BYTES))
    return fw.FrameWriter(path, tick=lambda: 1234), path


def header(path):
    return struct.unpack("<IIIIIIQQ", path.read_bytes()[:40])


def test_frames_are_published_after_they_are_written(tmp_path):
    import frame_writer as fw

    writer, path = make_writer(tmp_path)
    assert header(path)[0] == 0  # nothing published yet
    with writer.next_slot() as slot:
        slot[:] = b"\x07" * fw.FRAME_BYTES
    assert header(path)[6] == 0  # written, still not visible
    writer.publish()
    writer.close()

    magic, version, width, height, latest, _, sequence, tick = header(path)
    assert (magic, version, width, height) == (fw.MAGIC, fw.VERSION, fw.WIDTH, fw.HEIGHT)
    assert (sequence, tick) == (1, 1234)
    start = fw.HEADER_BYTES + latest * fw.FRAME_BYTES
    assert path.read_bytes()[start:start + fw.FRAME_BYTES] == b"\x07" * fw.FRAME_BYTES


def test_the_slot_being_written_is_never_the_published_one(tmp_path):
    writer, path = make_writer(tmp_path)
    for _ in range(7):
        with writer.next_slot():
            pass
        before = writer._slot
        writer.publish()
        with writer.next_slot():
            assert writer._slot != before
        writer.publish()
    writer.close()


def test_short_frame_file_is_rejected(tmp_path):
    import frame_writer as fw

    path = tmp_path / "frames"
    path.write_bytes(b"short")
    with pytest.raises(ValueError):
        fw.FrameWriter(path)


def test_read_exactly_survives_split_reads():
    import frame_writer as fw

    class Chunks(io.RawIOBase):
        def __init__(self, data):
            self.data = data

        def readinto(self, buffer):
            count = min(3, len(buffer), len(self.data))
            buffer[:count] = self.data[:count]
            self.data = self.data[count:]
            return count

    target = bytearray(8)
    with memoryview(target) as view:
        assert fw.read_exactly(Chunks(b"abcdefgh"), view) is True
    assert target == b"abcdefgh"
    with memoryview(bytearray(8)) as view:
        assert fw.read_exactly(Chunks(b"abc"), view) is False


def test_stdout_carries_only_video():
    import camera_receiver

    command = camera_receiver.build_command("ffmpeg", "10.0.2.2", 9000)
    assert "-progress" not in command
    assert command[-3:] == ["-f", "rawvideo", "pipe:1"]
    assert "tcp://10.0.2.2:9000?timeout=10000000" in command
    assert "scale=1280:720,fps=30,format=nv12" in command


def test_missing_frame_file_asks_for_the_install(tmp_path, monkeypatch):
    import camera_receiver
    from client_settings import ClientSettings
    from shared.receiver import DeviceError

    monkeypatch.setattr(camera_receiver, "frame_path", lambda: tmp_path / "absent")
    receiver = camera_receiver.CameraReceiver(ClientSettings(), lambda *a: None, lambda *a: None)
    with pytest.raises(DeviceError, match="Install Camera"):
        receiver.prepare()


def test_frame_server_can_only_read():
    import camera_installer as ci

    grants = ci.acl_grants()
    assert f"*{ci.SID_LOCAL_SERVICE}:(OI)(CI)RX" in grants
    assert f"*{ci.SID_USERS}:(OI)(CI)M" in grants
    assert len(grants) == 4  # nothing else keeps access


def test_status_follows_the_installed_files(tmp_path, monkeypatch):
    import camera_installer as ci
    import frame_writer as fw

    bundled, installed = tmp_path / "bundled", tmp_path / "installed"
    bundled.mkdir()
    installed.mkdir()
    frames = tmp_path / "frames"
    monkeypatch.setattr(ci, "bundled_dir", lambda: bundled)
    monkeypatch.setattr(ci, "install_dir", lambda: installed)
    monkeypatch.setattr(ci, "frame_path", lambda: frames)

    (bundled / ci.DLL).write_bytes(b"new")
    assert ci.camera_status() == ci.CameraStatus.MISSING

    (installed / ci.DLL).write_bytes(b"old")
    frames.write_bytes(bytes(fw.FILE_BYTES))
    assert ci.camera_status() == ci.CameraStatus.OUTDATED

    (installed / ci.DLL).write_bytes(b"new")
    assert ci.camera_status() == ci.CameraStatus.READY

    frames.write_bytes(b"short")
    assert ci.camera_status() == ci.CameraStatus.OUTDATED


def test_a_loaded_dll_is_replaced_by_renaming_it(tmp_path, monkeypatch):
    import camera_installer as ci

    source, target = tmp_path / "source", tmp_path / "target.dll"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    real_replace = ci.os.replace

    def locked_once(src, dst):
        if Path(dst) == target and Path(src).name.endswith(".new") and target.exists():
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(ci.os, "replace", locked_once)
    ci._replace_file(source, target)
    assert target.read_bytes() == b"new"
    assert [p.read_bytes() for p in tmp_path.glob("*.old")] == [b"old"]


def test_setup_failure_is_reported_to_the_unelevated_app(tmp_path, monkeypatch):
    import camera_installer as ci

    monkeypatch.setattr(ci, "data_dir", lambda: tmp_path)

    def fail():
        raise ci.InstallError("no way")

    assert ci.run_action(fail) == 1
    assert ci._read_result() == "no way"
    assert ci.run_action(lambda: None) == 0


def test_client_updates_itself_not_the_server(tmp_path):
    from shared import updater

    installer = updater.WindowsInstaller(tmp_path / "x.exe", asset_name=updater.WINDOWS_CLIENT_ASSET)
    assert installer.asset_name == "BroadcastClient.exe"
    assert updater.WindowsInstaller(tmp_path / "x.exe").asset_name == "BroadcastServer.exe"


def test_windows_connection_timeout_is_named():
    from shared.receiver import classify_failure

    assert classify_failure(["Error opening input: Error number -138 occurred"], "h", 1) == (
        "No response from h:1", False,
    )
