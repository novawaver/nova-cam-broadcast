# NVIDIA Broadcast Bridge

Use the NVIDIA Broadcast camera and microphone from a Windows 11 host inside a VirtualBox VM, without OBS Studio.

| Application | Runs on | Job |
| --- | --- | --- |
| **Broadcast Server** | Windows host | Captures both devices and serves them on TCP ports 9000 and 9001 |
| **Broadcast Client** | Ubuntu guest | Receives both streams and exposes `/dev/video10` and a virtual microphone |
| **Broadcast Client for Windows** | Windows 11 guest | Receives the camera stream and exposes a virtual camera |

```text
Windows                                          Ubuntu (VirtualBox NAT)
Camera     -> FFmpeg -> MPEG-2/MPEG-TS  TCP 9000 -> FFmpeg -> /dev/video10
Microphone -> FFmpeg -> raw PCM s16le   TCP 9001 -> FFmpeg -> null sink -> virtual source
```

Each stream has its own FFmpeg process and supervising thread, so one failing or stopping does not affect the other.

## Windows setup

The easiest way is to download `BroadcastServer.exe` from the [latest release](https://github.com/novawaver/nova-cam-broadcast/releases/latest) and run it. You need FFmpeg installed and NVIDIA Broadcast running. Windows SmartScreen may warn you the first time. Choose **More info**, then **Run anyway**.

To run from source instead, you also need Python 3.11+ from python.org:

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

Download `nvidia-broadcast-bridge-ubuntu.tar.gz` from the [latest release](https://github.com/novawaver/nova-cam-broadcast/releases/latest) into the VM, or clone this repository. Then run this as your normal user:

```bash
mkdir nvidia-broadcast-bridge
tar -xzf nvidia-broadcast-bridge-ubuntu.tar.gz -C nvidia-broadcast-bridge
cd nvidia-broadcast-bridge
bash scripts/install_ubuntu.sh
```

If you cloned the repository, skip the `mkdir` and `tar` steps and run the last line from the repository folder.

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

## Updates

Both apps check GitHub for a new release a few seconds after launch. If there is one, you choose **Update Now**, **Skip This Version** or **Later**. Nothing installs without your click, because updating stops any active streams and restarts the app. **Check for Updates** runs the same check on demand, and you can turn off the launch check in **Settings**.

Update the server and client to the same version.

| How you run it | What updating does |
| --- | --- |
| `BroadcastServer.exe` | Replaces the exe and restarts. The old file is removed on the next launch |
| Installed with `install_ubuntu.sh` | Replaces the app folder, updates Python packages if needed, and restarts |
| From source, or a PyInstaller build on Ubuntu | Tells you a new version exists and opens the release page |

If an update fails, your current version keeps working. The Ubuntu updater doesn't change system files such as the driver setup or root helper. If a release changes those, its notes will ask you to rerun `install_ubuntu.sh`.

The updater only downloads over HTTPS from GitHub, and it checks each file against the release's `SHA256SUMS.txt`. This detects corrupted or altered downloads, but the checksums come from the same GitHub release as the files. Anyone who gains control of the GitHub account could therefore publish a malicious update, so protect the account with two-factor authentication.

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

## Windows 11 VM

To use the camera in a Windows 11 VM, keep Broadcast Server running on the host as usual. In the VM:

1. Install FFmpeg: `winget install Gyan.FFmpeg`.
2. Download `BroadcastClient.exe` from the [latest release](https://github.com/novawaver/nova-cam-broadcast/releases/latest) and run it.
3. Click **Install Camera** and approve the Windows prompt. You only do this once.
4. Click **Connect Camera**. The status turns green when video is flowing.
5. Choose **NVIDIA Broadcast (Windows Virtual Camera)** in your app.

If the checks show **Camera privacy** in red, turn on camera access in **Settings > Privacy & security > Camera**, including **Let desktop apps access your camera**.

**The microphone isn't carried by this client.** Windows needs a signed audio driver for a virtual microphone. Turn on **Enable Audio Input** in the VM's VirtualBox audio settings to use the host's default microphone instead.

The camera is 1280 × 720 at 30 FPS. Without a live stream, it shows black. **Remove Camera** uninstalls it. Any local user on the VM can read the camera's frames, because they pass through a shared file in `C:\ProgramData\NvidiaBroadcastBridge`.

To build from source, install the Visual Studio C++ build tools, then run `scripts\build_windows.ps1` followed by `scripts\build_windows_client.ps1`.

## Publishing a release

Releases are built and published automatically by [.github/workflows/release.yml](.github/workflows/release.yml). You never upload files by hand.

1. Set the new version in `shared/version.py`.
2. Commit, tag and push. The tag must match the version:
   ```bash
   git add -A
   git commit -m "Release 0.1.1"
   git tag v0.1.1
   git push origin main v0.1.1
   ```

GitHub then builds both exes, runs the tests, signs the exes if signing is set up, and publishes the release with all four files. Watch it on the repository's **Actions** tab. If the tag and `shared/version.py` don't match, or a test fails, nothing is published.

To build the same files locally without publishing, run `powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1`. They're written to `dist\release`.

## Code signing

Signing uses [Azure Artifact Signing](https://learn.microsoft.com/azure/artifact-signing/), formerly Trusted Signing. This is a different product from Azure Artifacts, the package feed service. You need an Artifact Signing account, a completed identity validation and a **Public Trust** certificate profile.

Until signing is set up, releases are published unsigned and the workflow shows a warning. Once it's set up, a signing failure stops the release, so an unsigned exe can't slip out.

GitHub signs in to Azure with OpenID Connect, so no password or secret is stored.

1. **Create an app.** In Microsoft Entra ID, open **App registrations**, create one named `nvidia-broadcast-bridge-release`, and note its **Application (client) ID** and **Directory (tenant) ID**.
2. **Trust this workflow.** In that app, open **Certificates & secrets > Federated credentials > Add credential**, choose **GitHub Actions deploying Azure resources**, and enter:
   - Organization: `novawaver`
   - Repository: `nova-cam-broadcast`
   - Entity: **Environment**
   - Environment: `release`
3. **Grant signing.** On your certificate profile, open **Access control (IAM)** and assign the **Artifact Signing Certificate Profile Signer** role to the app. Search for it by name, because only users are listed by default.
4. **Configure the repository.** In GitHub, open **Settings > Secrets and variables > Actions > Variables** and add these repository variables:

   | Variable | Value |
   | --- | --- |
   | `AZURE_CLIENT_ID` | Application (client) ID from step 1 |
   | `AZURE_TENANT_ID` | Directory (tenant) ID from step 1 |
   | `AZURE_SUBSCRIPTION_ID` | The subscription that holds the signing account |
   | `ARTIFACT_SIGNING_ENDPOINT` | The account's endpoint, such as `https://eus.codesigning.azure.net/` |
   | `ARTIFACT_SIGNING_ACCOUNT` | The signing account name |
   | `ARTIFACT_SIGNING_PROFILE` | The certificate profile name |

   None of these are secrets. Setting `ARTIFACT_SIGNING_ACCOUNT` is what switches signing on, so add it last.

Signing proves who published the exe, but it doesn't remove the SmartScreen warning straight away. SmartScreen builds reputation as people download and run a file, so a new release can still show the warning at first.

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
scripts/         installer, build and release scripts, privileged helper, polkit policy
tests/           automated tests
```
