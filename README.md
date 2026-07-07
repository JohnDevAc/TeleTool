# TeleTool

TeleTool is a Raspberry Pi 5 broadcast utility for controlling Tvheadend streams, publishing the selected service as NDI, and routing the active stereo audio to a local line-level output device. The audio path is intended for a Dante AVIO USB adapter, which appears to the Pi as a stereo USB playback device.

AES67/RTP output has been removed. The current audio workflow is local line output through ALSA/GStreamer.

## What The App Does

- Select a Tvheadend channel and publish it as an NDI source.
- Keep the NDI stream supervised and restart it if the live Tvheadend stream stalls.
- Route the same active stream audio to a local stereo output device.
- Manage multiple TeleTool units from the Fleet Manager page.
- Detect only relevant audio output devices:
  - Dante AVIO or suitable USB audio output
  - Pi analogue/headphones output where present
- Hide HDMI outputs and noisy ALSA aliases from the audio dropdown.
- Configure network, hostname, power actions, updates, and advanced NDI settings from the web UI.
- Run a guided Tvheadend DVB-T/T2 setup flow with stalled-scan handling.
- Build a shrunk Raspberry Pi SD-card golden-master image for cloning.

## Web UI

The FastAPI app runs on port `8000`.

```text
http://<pi-hostname-or-ip>:8000/
```

Main pages:

- `/` - NDI control and Tvheadend setup
- `/audio` - local line-level audio output control
- `/manager` - Fleet Manager for multiple TeleTool units
- `/system` - hostname, network, power, and app configuration

## Normal Operation

1. Open the main page at `http://<pi>:8000/`.
2. Select a Tvheadend channel.
3. Enter a unique NDI source name.
4. Click `Start NDI`.
5. Confirm the NDI source appears on the network.
6. Open `/audio`.
7. Connect the Dante AVIO USB adapter if it is not already attached.
8. Click `Refresh devices`.
9. Select the Dante AVIO USB audio output.
10. Set output level with the slider.
11. Click `Start Audio`.

The audio output branch is tied to the active NDI pipeline. Start NDI first, then start audio.

The audio level slider is stored as a linear GStreamer volume value:

- `80%` is the default and is about `-1.9 dB`.
- `100%` is unity gain, `0.0 dB`.
- `50%` is about `-6.0 dB`.
- `0%` is muted.

## Fleet Manager

Open `/manager` to monitor multiple TeleTool units from one primary unit. Add each unit by IP address or hostname. Each unit appears as a box showing system status, stream status, IP/hostname, NDI stream name, and the current channel when an NDI stream is running.

The primary unit also appears in the Fleet Manager. If a TeleTool is adopted by an active Fleet Manager, its own management page is disabled and redirects back to the managing unit until the adoption lease expires.

## Tvheadend Setup Flow

The TV Setup panel on `/` can rebuild Tvheadend channel data for a DVB-T/T2 region.

Important: this flow is destructive. It deletes current Tvheadend channels/services, applies the selected predefined mux region, starts a scan, and maps services back to channels.

When no previous region has been saved, the region dropdown prefers Tvheadend's preconfigured mux list labelled `Generic: auto-Default` where available.

Some RF environments can cause Tvheadend to stall near the end of a scan. TeleTool monitors mux progress during TV Setup:

- If the scan finishes normally, services are mapped and the setup shows `Complete`.
- If scan progress stalls but services have been found, those services are mapped and the setup shows `Partial`.
- If scan progress stalls and no services have been found, the setup shows `Failed`.

The default stalled-scan threshold is `tvh_scan_stall_timeout_s = 120` seconds without mux progress. The overall scan timeout defaults to `tvh_scan_timeout_s = 600` seconds. These can be adjusted in `config.json` if a site needs longer scan windows.

Use it when preparing a fresh Pi or when intentionally rebuilding the tuner/channel setup.

## System Page

Open `/system` for:

- Restarting the TeleTool service.
- Updating the program from the latest server version on either the Main or Dev branch.
- Rebooting the Pi.
- Changing hostname.
- Viewing or applying network settings.
- Editing advanced NDI config such as delay, buffer, reconnect, startup grace, and stall timeout.

Network changes can briefly disconnect the web UI if the Pi changes IP address.

## Project Layout

- `app.py` - FastAPI web/API entrypoint.
- `gst_ndi.py`, `gst_base.py` - GStreamer pipeline control.
- `tvh.py` - Tvheadend API client.
- `static/` - web UI.
- `config.example.json` - default config template committed to git.
- `config.json` - local runtime config used by the app, ignored by git.
- `.env.local` - local Pi SSH/sync credentials, ignored by git.
- `.vscode/tasks.json` - VS Code tasks for syncing and managing the Pi.
- `deploy/systemd/tvh_ndi_bridge.service` - systemd service file.
- `scripts/pi_sync.ps1` - Windows/VS Code sync script.
- `scripts/pi_setup.sh` - Raspberry Pi dependency and service setup.
- `scripts/pi_make_golden_image.sh` - golden-master SD-card image builder.
- `golden-images/` - local image artifacts, ignored by git.

## Git Safety

The repository uses `main` for stable releases and `dev` for development builds. The System page update control lets a device update from either branch.

These files are intentionally not committed:

- `.env.local`
- `config.json`
- `golden-images/`
- Python cache files

`config.example.json` is the committed template. Copy or sync it to `config.json` only when intentionally resetting a device config.

## Raspberry Pi Requirements

The setup script installs common Raspberry Pi OS packages for:

- Python virtual environments.
- PyGObject.
- GStreamer base/good/bad/ugly/libav and ALSA plugins.
- ALSA device discovery tools.
- Avahi helpers.

NDI support still requires an ARM64 NDI runtime/GStreamer plugin that provides `ndisink`. After installing it, this should succeed on the Pi:

```sh
gst-inspect-1.0 ndisink
```

Tvheadend is expected at `http://127.0.0.1:9981` by default. Change `tvh_base_url` in `config.json` if Tvheadend is elsewhere.

For Dante AVIO USB output, connect the adapter to the Pi and open `/audio`. The Audio output dropdown only lists suitable line-output devices. If the dropdown is empty, check whether ALSA can see the adapter:

```sh
aplay -l
```

## VS Code Raspberry Pi Sync

Install the recommended VS Code extensions when prompted:

- Python
- Remote - SSH
- PowerShell

Create `.env.local` in the project root for local sync credentials:

```text
TELETOOL_PI_HOST=192.168.0.142
TELETOOL_PI_USER=admin
TELETOOL_PI_PASSWORD=<your-pi-password>
TELETOOL_PI_PATH=/home/admin/tvh_ndi_bridge
```

Do not commit `.env.local`.

Useful tasks:

- `Pi: Sync project` - copy code and static assets to the Pi while preserving remote `config.json`.
- `Pi: Sync project including config` - copy everything including `config.json`.
- `Pi: Setup dependencies/service` - install packages, create `.venv`, install systemd service.
- `Pi: Restart service` - restart `tvh_ndi_bridge.service`.
- `Pi: Tail service logs` - follow service logs over SSH.
- `Local: Run API` - run the FastAPI app locally with uvicorn.

Normal deployment flow:

1. Run `Pi: Sync project`.
2. Run `Pi: Setup dependencies/service` after a fresh flash or package change.
3. Run `Pi: Restart service`.
4. Open `http://<pi>:8000/`.

The normal sync task preserves `config.json` on the Pi because the web UI writes device-specific settings there.

## Service Commands On The Pi

```sh
sudo systemctl restart tvh_ndi_bridge.service
systemctl status tvh_ndi_bridge.service
journalctl -u tvh_ndi_bridge.service -f
```

Tvheadend:

```sh
systemctl status tvheadend
journalctl -u tvheadend -f
```

## Golden Master Images

The project includes a repeatable SD-card image builder:

```sh
sudo env COMPRESS=xz ROOT_MIN_MIB=12288 ROOT_MARGIN_MIB=4096 \
  bash scripts/pi_make_golden_image.sh /home/admin/golden-master teletool-pi5-golden-$(date +%Y%m%d-%H%M%S)
```

The builder creates a smaller two-partition Raspberry Pi image instead of cloning the entire SD card byte-for-byte. It:

- Creates a sparse image with a FAT boot partition and ext4 root partition.
- Copies the live root filesystem and boot partition into the image.
- Stops `tvh_ndi_bridge` and `tvheadend` during the copy for a cleaner snapshot.
- Updates `fstab` and `cmdline.txt` PARTUUID references inside the image.
- Adds a one-shot first-boot service that expands the root filesystem to fill the target SD card.
- Clears machine-id/random-seed/log noise so clones are cleaner.
- Runs `e2fsck` on the generated root filesystem.
- Compresses the image to `.img.xz`.
- Writes `.sha256` and `.manifest` sidecar files.

Generated image artifacts are intentionally ignored by git.

Hosted golden-master download:

[Download TeletoolBase.img.xz](https://www.johnlightfoot.biz/TeletoolBase.img.xz)

Current golden-master artifact:

```text
/home/admin/golden-master/TeletoolBase.img.xz
```

Current image details:

- Created: `2026-07-07T20:01:15+01:00`
- Decompressed image size: `12,868 MiB`
- Compressed image size: about `1.9 GB`
- SHA-256: `1bf7f0b337c0f3d5fd876896c354d8a306724bcaa3ba990e400fe35f82aef315`

Verify the image on Windows:

```powershell
Get-FileHash -Algorithm SHA256 .\golden-images\TeletoolBase.img.xz
Get-Content .\golden-images\TeletoolBase.img.xz.sha256
```

The two hashes should match.

## Flashing The Golden Master

Use Raspberry Pi Imager or balenaEtcher and select the `.img.xz` file as a custom image.

Recommended first boot checklist:

1. Flash the `.img.xz` image to a new SD card.
2. Boot the Pi and wait a few minutes for first-boot root expansion.
3. Find the Pi on the network by hostname, router DHCP table, or direct IP scan.
4. SSH into the Pi.
5. Change the default password immediately.
6. Open `http://<pi>:8000/system`.
7. Set hostname and network settings for that unit.
8. Confirm `tvh_ndi_bridge` and `tvheadend` are active.
9. Open `http://<pi>:8000/`.
10. Run Tvheadend setup if the tuner/channel list needs to be rebuilt.
11. Start NDI and verify the source appears on the network.
12. Connect the Dante AVIO USB adapter, open `/audio`, refresh devices, and start line output.

The first-boot expansion service removes itself after it runs.

## Copying Golden Image Artifacts Off The Pi

Images are created on the Pi under `/home/admin/golden-master/`.

From Windows:

```powershell
pscp admin@192.168.0.142:/home/admin/golden-master/TeletoolBase.img.xz .\golden-images\
pscp admin@192.168.0.142:/home/admin/golden-master/TeletoolBase.img.xz.sha256 .\golden-images\
pscp admin@192.168.0.142:/home/admin/golden-master/TeletoolBase.img.xz.manifest .\golden-images\
```

If using SSH keys or a different username/host, adjust the command accordingly.

## Local Development

On Windows, full local runtime may be limited because PyGObject and GStreamer are easiest on Raspberry Pi OS. Lightweight syntax checks still work:

```powershell
python -m compileall app.py gst_ndi.py gst_base.py tvh.py
```

On the Pi:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

## Troubleshooting

Check service status:

```sh
systemctl status tvh_ndi_bridge.service
journalctl -u tvh_ndi_bridge.service -n 120 --no-pager
```

Check NDI plugin:

```sh
gst-inspect-1.0 ndisink
```

Check audio devices:

```sh
aplay -l
curl http://127.0.0.1:8000/api/audio/devices
```

If `/api/audio/devices` is empty but `aplay -l` only shows HDMI devices, that is expected. HDMI is hidden from the TeleTool audio dropdown. Connect the Dante AVIO USB adapter and refresh devices.

Check Tvheadend:

```sh
curl http://127.0.0.1:9981/api/serverinfo
systemctl status tvheadend
```

If the web UI cannot change network settings or reboot the Pi, install the helper once:

```sh
sudo ./install_network_privileges.sh
```
