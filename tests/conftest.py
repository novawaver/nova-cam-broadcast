import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "windows-server", ROOT / "ubuntu-client"):
    sys.path.insert(0, str(path))
