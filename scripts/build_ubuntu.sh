#!/usr/bin/env bash
# Builds a standalone folder at dist/BroadcastClient. The target machine still
# needs the system parts from install_ubuntu.sh: driver, helper, FFmpeg, pactl.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r ubuntu-client/requirements.txt pyinstaller

.venv/bin/pyinstaller --noconfirm --clean --windowed --onedir \
    --name BroadcastClient --paths . --paths ubuntu-client \
    ubuntu-client/main.py

echo "Built: $REPO/dist/BroadcastClient/BroadcastClient"
