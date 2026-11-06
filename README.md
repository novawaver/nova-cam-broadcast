# NVIDIA Broadcast Bridge

Use the NVIDIA Broadcast camera and microphone from a Windows 11 host inside an Ubuntu VirtualBox VM, without OBS Studio.

| Application | Runs on | Job |
| --- | --- | --- |
| **Broadcast Server** | Windows host | Captures both devices and serves them on TCP ports 9000 and 9001 |
| **Broadcast Client** | Ubuntu guest | Receives both streams and exposes `/dev/video10` and a virtual microphone |

```text
Windows                                          Ubuntu (VirtualBox NAT)
Camera     -> FFmpeg -> MPEG-2/MPEG-TS  TCP 9000 -> FFmpeg -> /dev/video10
Microphone -> FFmpeg -> raw PCM s16le   TCP 9001 -> FFmpeg -> null sink -> virtual source
```

Each stream has its own FFmpeg process and supervising thread, so one failing or stopping does not affect the other.

## Windows setup

You need Python 3.11+ from python.org, FFmpeg, and NVIDIA Broadcast running.

Run from source:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r windows-server\requirements.txt
.venv\Scripts\python windows-server\main.py
```

Or build a standalone executable at `dist\BroadcastServer.exe`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

FFmpeg is found automatically on `PATH`, next to the executable, or in the usual WinGet, Chocolatey, Scoop and `C:\ffmpeg\bin` locations. Otherwise set its path in **Settings**.

## Ubuntu setup

Copy or clone this repository into the VM, then run this as your normal user:

```bash
bash scripts/install_ubuntu.sh
```

The installer:

- installs the dependencies and verifies that the kernel headers and DKMS module match the running kernel
- configures `/dev/video10` to load at boot
- adds a privileged helper and a launcher named **NVIDIA Broadcast Bridge**

If it tells you that you were added to the `video` group, log out and back in.

For a standalone folder at `dist/BroadcastClient/`, run `bash scripts/build_ubuntu.sh`. It still needs the system parts from the installer, so run that on the target machine too.

## Daily use

1. On Windows, open Broadcast Server and click **START BOTH**. Each stream shows yellow, *Listening, waiting for Ubuntu*.
2. In Ubuntu, open NVIDIA Broadcast Bridge and click **CONNECT BOTH**. Both apps turn green.
3. In your Ubuntu app, select the camera **NVIDIA Broadcast** and the microphone **NVIDIA Broadcast Microphone**.

The order doesn't matter: the client keeps retrying until the server is up.

| Color | Meaning |
| --- | --- |
| Gray | Stopped |
| Yellow | Starting, waiting or reconnecting |
| Green | Server: a client socket is established. Client: media is flowing |
| Red | Error; the reason is shown on the card and in the log |

Run `python ubuntu-client/main.py --diagnose` on Ubuntu, or click **Refresh Checks**, to see which part of the setup is missing.

### Command-line use

The GUI isn't required. Add `--camera` or `--mic` to run a single stream.

```powershell
python windows-server\main.py --list-devices
python windows-server\main.py --cli
```

```bash
python ubuntu-client/main.py --cli --host 10.0.2.2
```

Settings and logs are stored in `%APPDATA%\nvidia-broadcast-bridge` on Windows and `~/.config/nvidia-broadcast-bridge` on Ubuntu.

## Security

The streams are **unencrypted and unauthenticated**. Anyone who can reach the ports can view your camera and hear your microphone, so never expose ports 9000 and 9001 to the internet.

**Prefer loopback if your VM supports it.** VirtualBox NAT can map `10.0.2.2` to the host's loopback, in which case nothing else on your network can reach the streams. Power off the VM and run:

```powershell
VBoxManage modifyvm "<VM name>" --nat-localhostreachable1 on
```

Then set **Bind address** to `127.0.0.1` in the server's Settings. If the client can no longer connect, change it back.

**Otherwise, use `0.0.0.0` with a firewall rule.** This is the default because it matches the setup already proven to work. Allow the ports only on private networks, and do not forward them on your router:

```powershell
New-NetFirewallRule -DisplayName "NVIDIA Broadcast Bridge" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 9000,9001 -Profile Private -RemoteAddress LocalSubnet
```

## How it behaves

**Privileges.** Neither GUI runs as root. Only `/usr/local/libexec/nvbb-camera-helper` does, through polkit. It takes no arguments and does one thing: create `/dev/video10`. It's needed only if the device is missing, and because the module normally loads at boot, you'll rarely see a password prompt.

**Existing devices.** The client never unloads or reloads `v4l2loopback`. If the module is already loaded without `/dev/video10`, the helper adds a device beside the existing ones. A correctly configured `/dev/video10` is reused untouched, and it stays after exit because other apps may be using it.

**Virtual microphone.** The chain is null sink → monitor → remap source. The remap source matters because Chrome hides sink monitors from its microphone list. A null sink has no hardware output, so the audio never reaches your speakers. If the new sink becomes the default output, the client restores your previous default.

The client finds its modules by their unique names, `nvbb_mic_sink` and `nvbb_mic`. That lets it reuse leftovers after a crash, remove duplicates, and unload both on exit without touching anything else. If the remap source can't be created, it falls back to the sink monitor and says so in the log. In that case, pick the input with `pavucontrol` for apps that hide monitors.

**Connection state.** The client never test-connects to the server, because `listen=1` accepts exactly one client and a probe would end the stream. It works out reachability from FFmpeg's own connection errors instead.

**Process cleanup.** Windows uses a kill-on-close job object and Linux uses `PR_SET_PDEATHSIG`, so FFmpeg exits with the app even after a crash.

## Known limitations

- **Mid-call reconnects.** With `exclusive_caps=1`, the camera is a capture device only while FFmpeg is writing to it. An app that opens the camera during an outage may not list it, and may need the camera reselected after a long one.
- **Audio clock drift.** The Windows and Ubuntu audio clocks aren't synchronized, so very long sessions can slowly gain latency. **Disconnect Microphone** then **Connect Microphone** resets it. If you hear crackling on a busy VM, raise `PLAYBACK_BUFFER_MS` in `ubuntu-client/microphone_manager.py`.
- **One client per stream.** This follows from the `listen=1` transport.

## Acceptance test

Run this end to end on the real setup. To exercise a real audio path, record and play back your voice in the [WebRTC record sample](https://webrtc.github.io/samples/src/content/getusermedia/record/) or a test call. An audio source existing doesn't prove the microphone works.

| # | Test | Expected |
| --- | --- | --- |
| 1 | Launch both apps | Both windows open |
| 2 | Open the server's device lists | NVIDIA Broadcast camera and microphone are preselected |
| 3 | START BOTH, then CONNECT BOTH | Both streams are green in both apps |
| 4 | `v4l2-ctl -d /dev/video10 --all` while connected | Reports a video capture device |
| 5 | Open a browser device test | **NVIDIA Broadcast** and **NVIDIA Broadcast Microphone** are selectable |
| 6 | Record video and voice, then play it back | Both work at once, and nothing plays through the speakers while recording |
| 7 | Stop only the camera, then only the mic, on each side | The other stream keeps working |
| 8 | Close and reopen the client three times; run `pactl list short sources` | Exactly one `nvbb_mic` |
| 9 | Kill the client with `kill -9`, reopen it, connect; repeat the check | Still exactly one `nvbb_mic`, and no leftover `ffmpeg` |
| 10 | Disconnect the VM's network cable in VirtualBox for 30 s, then reconnect | Yellow, then green with no clicks |
| 11 | Quit both apps; check Task Manager and `pgrep ffmpeg` | No FFmpeg processes remain |
| 12 | Occupy port 9000, then start the camera | Red, naming the process that holds the port |
| 13 | Close NVIDIA Broadcast while streaming | The camera reports an error; the microphone is handled independently |

## Development

```bash
pip install pytest
python -m pytest tests
```

The tests cover parsing, command construction and worker supervision with a stand-in for FFmpeg. They don't need a camera, FFmpeg, PipeWire or a GUI.

```text
shared/          configuration, logging, FFmpeg process control, worker base, Qt widgets
windows-server/  GUI, device detection, TCP port monitor, stream supervision
ubuntu-client/   GUI, camera and microphone devices, reconnection, diagnostics
scripts/         installer, build scripts, privileged helper, polkit policy
tests/           automated tests
```
