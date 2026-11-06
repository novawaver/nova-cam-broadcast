#!/usr/bin/env bash
# Installs the Ubuntu client for the current user. Run as your normal user;
# sudo is used only for the system steps. Safe to run again.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/nvidia-broadcast-bridge"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
KERNEL="$(uname -r)"
MODPROBE_CONF=/etc/modprobe.d/nvbb-v4l2loopback.conf
MODULES_CONF=/etc/modules-load.d/nvbb-v4l2loopback.conf

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo."
command -v apt-get >/dev/null || die "This installer needs an apt-based system (Ubuntu)."

say "Installing packages"
sudo apt-get update
# Headers are installed separately so the error can name the actual cause.
sudo apt-get install -y \
    ffmpeg v4l-utils dkms v4l2loopback-dkms v4l2loopback-utils \
    pulseaudio-utils python3-venv libxcb-cursor0 polkitd pkexec
sudo apt-get install -y "linux-headers-$KERNEL" \
    || die "No headers package for kernel $KERNEL. Update the kernel, reboot, and retry."

say "Checking the driver against kernel $KERNEL"
[ -e "/lib/modules/$KERNEL/build" ] || die "Kernel headers are missing for $KERNEL."
if ! modinfo v4l2loopback >/dev/null 2>&1; then
    dkms status v4l2loopback >&2 || true
    die "v4l2loopback did not build for kernel $KERNEL. See the dkms status above."
fi
if ! dkms status v4l2loopback 2>/dev/null | grep -q "$KERNEL"; then
    warn "dkms lists no v4l2loopback build for $KERNEL; a packaged module may be in use."
fi
if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -q enabled; then
    warn "Secure Boot is enabled. An unsigned DKMS module may be refused until its key is enrolled."
fi

say "Installing the camera helper"
sudo install -D -o root -g root -m 0755 "$REPO/scripts/nvbb-camera-helper" /usr/local/libexec/nvbb-camera-helper
sudo install -D -o root -g root -m 0644 "$REPO/scripts/org.nvbb.camera-helper.policy" \
    /usr/share/polkit-1/actions/org.nvbb.camera-helper.policy

say "Configuring the virtual camera"
# Do not override a v4l2loopback configuration that belongs to someone else.
OTHER_CONF="$(grep -ls 'options[[:space:]]\+v4l2loopback' /etc/modprobe.d/*.conf | grep -v "^$MODPROBE_CONF$" || true)"
if [ -n "$OTHER_CONF" ]; then
    warn "Keeping existing v4l2loopback options in: $OTHER_CONF"
    warn "Boot-time loading was not changed; the app creates /dev/video10 when needed."
else
    echo 'options v4l2loopback devices=1 video_nr=10 card_label="NVIDIA Broadcast" exclusive_caps=1' \
        | sudo tee "$MODPROBE_CONF" >/dev/null
    echo v4l2loopback | sudo tee "$MODULES_CONF" >/dev/null
fi
sudo /usr/local/libexec/nvbb-camera-helper

say "Checking device access"
if [ -w /dev/video10 ]; then
    echo "/dev/video10 is writable."
else
    sudo usermod -aG video "$USER"
    warn "Added $USER to the 'video' group. Log out and back in before connecting."
fi

say "Checking audio"
if pactl info >/dev/null 2>&1; then
    pactl info | grep '^Server Name:'
else
    warn "No PulseAudio-compatible server answered. Is pipewire-pulse running?"
fi

say "Installing the application"
mkdir -p "$APP_HOME/app"
rm -rf "$APP_HOME/app/shared" "$APP_HOME/app/ubuntu-client"
cp -r "$REPO/shared" "$REPO/ubuntu-client" "$APP_HOME/app/"
find "$APP_HOME/app" -type d -name __pycache__ -prune -exec rm -rf {} +
python3 -m venv "$APP_HOME/venv"
"$APP_HOME/venv/bin/pip" install --quiet --upgrade pip
"$APP_HOME/venv/bin/pip" install --quiet -r "$REPO/ubuntu-client/requirements.txt"

say "Creating the desktop launcher"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/nvidia-broadcast-bridge.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=NVIDIA Broadcast Bridge
Comment=Use NVIDIA Broadcast from Windows as a camera and microphone
Exec="$APP_HOME/venv/bin/python" "$APP_HOME/app/ubuntu-client/main.py"
Icon=camera-web
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=nvidia-broadcast-bridge
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" || true

say "Final check"
"$APP_HOME/venv/bin/python" "$APP_HOME/app/ubuntu-client/main.py" --diagnose || true

say "Done. Open 'NVIDIA Broadcast Bridge' from the applications menu."
