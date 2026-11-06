"""Assemble dist/release from the built exe and the repo sources.

Produces the exact file names the in-app updater looks for, plus SHA256SUMS.txt.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.updater import CHECKSUM_ASSET, UBUNTU_ASSET, WINDOWS_ASSET  # noqa: E402
from shared.version import __version__  # noqa: E402

UBUNTU_CONTENT = ("shared", "ubuntu-client", "scripts", "README.md")


def _clean(info: tarfile.TarInfo):
    if "__pycache__" in info.name or info.name.endswith(".pyc"):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def main() -> int:
    exe = ROOT / "dist" / WINDOWS_ASSET
    if not exe.is_file():
        print(f"Missing {exe}. Run scripts/build_windows.ps1 first.", file=sys.stderr)
        return 1

    output = ROOT / "dist" / "release"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)

    shutil.copy2(exe, output / WINDOWS_ASSET)
    with tarfile.open(output / UBUNTU_ASSET, "w:gz") as archive:
        for name in UBUNTU_CONTENT:
            archive.add(ROOT / name, arcname=name, filter=_clean)

    lines = []
    for name in (WINDOWS_ASSET, UBUNTU_ASSET):
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    (output / CHECKSUM_ASSET).write_text("".join(lines), encoding="ascii", newline="\n")

    print(f"Release v{__version__} ready in {output}")
    print("".join(lines), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
