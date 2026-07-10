# TeleTool

Fresh Raspberry Pi OS Lite installation (adds the signed TeleTool APT repository):
`wget -qO- https://johndevac.github.io/teletwat/apt-repo/install.sh | sudo sh`

If the TeleTool repository is already configured: `sudo apt-get update && sudo apt-get install teletool`

TeleTool is a FastAPI application for a Raspberry Pi 5 that bridges Tvheadend TV services to NDI and optional local line-level audio output. It is designed for DVB-T/T2 workflows where a selected Tvheadend channel is decoded with GStreamer, published as an NDI source, and monitored from a simple web UI.

The local audio path is intended for line-output devices such as a Dante AVIO USB adapter. HDMI outputs and noisy ALSA aliases are hidden from the audio device list.

## Features

- Select a Tvheadend channel and publish it as an NDI source.
- Supervise the live stream and restart the NDI pipeline if it stalls.
- Preserve interlaced sources by default; software deinterlacing is optional.
- Route the active stream audio to a local ALSA/GStreamer output device.
- Show live stream, RF, NDI, and audio status in the web UI.
- Run a guided Tvheadend DVB-T/T2 setup and channel mapping flow.
- Manage multiple TeleTool units from the Fleet Manager page.
- Configure hostname, network, software updates, reboot, and NDI/audio settings from the System page.
- Build optional Raspberry Pi SD-card golden images for cloning.

## Web UI

The app runs on port `8000`.

```text
http://<pi-hostname-or-ip>:8000/
```

Main pages:

- `/` - NDI control and Tvheadend setup
- `/audio` - local line-level audio output
- `/manager` - Fleet Manager for multiple TeleTool units
- `/system` - hostname, network, power, updates, and advanced settings

API reference: [API.md](API.md). FastAPI also exposes generated docs at `/docs`.

## Normal Operation

1. Open `http://<pi>:8000/`.
2. Select a Tvheadend channel.
3. Enter a unique NDI source name.
4. Click `Start NDI`.
5. Confirm the NDI source appears on the network.
6. Open `/audio` if local audio output is required.
7. Select the Dante AVIO or other suitable line-output device.
8. Set the output level and click `Start Audio`.

Audio output depends on the active NDI pipeline, so start NDI first. The default audio level is `0.8`, about `-1.9 dB`; `1.0` is unity gain.

## Tvheadend Setup

The TV Setup panel on `/` can rebuild Tvheadend DVB-T/T2 channel data.

This is destructive: it deletes current Tvheadend channels/services, applies the selected predefined mux region, starts a scan, and maps services back to channels.

When no previous region has been saved, TeleTool prefers `United Kingdom: auto DVB-T/T2` where available. This is slower than selecting a specific transmitter, but includes DVB-T2 HD multiplex parameters that Tvheadend's built-in `Generic: auto-Default` list can miss.

TeleTool monitors scan progress and will map found services if Tvheadend stalls after finding usable services. Setup status is shown as `Complete`, `Partial`, or `Failed`.

## Configuration

Runtime settings live in `config.json`, which is intentionally ignored by git because the web UI edits it on the Pi. Defaults are committed in `config.example.json`.

Common settings:

- `tvh_base_url` - Tvheadend API base URL, default `http://127.0.0.1:9981`
- `tvh_dvbt_scanfile` - default DVB-T/T2 scanfile
- `tvh_stream_profile` - Tvheadend stream profile, usually `pass`
- `ndi_default_name` - default NDI source name
- `ndi_delay_ms` - NDI audio/video delay
- `ndi_deinterlace` - optional software deinterlace
- `ndi_stall_timeout_s` - supervisor stall timeout after frames have rendered
- `lineout_default_device` and `lineout_volume` - local audio defaults

Use `/system` or `POST /api/config/ui` to change UI-managed settings.

## Raspberry Pi APT Installation

For a fresh 64-bit Raspberry Pi OS Lite installation based on Debian Trixie,
the recommended installer is the signed TeleTool APT repository:

```sh
curl -fsSL https://johndevac.github.io/teletwat/apt-repo/install.sh | sudo sh
```

The repository bootstrap uses the TeleTool terminal UI, adds the signed package
source, runs `apt-get update`, and installs `teletool` with its Tvheadend,
GStreamer, Python, audio, and network dependencies. The completion screen shows
the unit Web UI link. If the NDI runtime is missing, open that link and upload
the ARM64 `libndi.so.6` file as directed. TeleTool V1.7.2 is distributed with
installer version 1.0. Package download and installation details are written to
`/var/log/teletool-installer.log` while the full-screen terminal UI shows the
overall percentage and a clean patience message without exposing individual
package names. The terminal palette uses TeleTool yellow and blue derived from
the logo, with a compact TeleTool ASCII banner at the top of each screen.

After the repository is configured, normal package commands are sufficient:

```sh
sudo apt-get update
sudo apt-get install teletool
sudo apt-get upgrade
```

Package-installed units can also install the latest signed TeleTool package
from the System page. The Web UI runs the update in a dedicated system service
so `apt` can safely restart TeleTool while the package is being configured.

The package currently targets `arm64` and Raspberry Pi OS Trixie. See
[`packaging/README.md`](packaging/README.md) for build, signing, publishing, and
repository maintenance instructions.

## Clean Raspberry Pi Setup

The checkout-based bootstrap remains available for development or recovery. Run
it as the normal service user, for example `admin`:

```sh
curl -fsSL https://raw.githubusercontent.com/JohnDevAc/teletwat/dev/scripts/pi_full_setup.sh | bash
```

The full setup script installs OS packages, Tvheadend, DVB scan tables,
GStreamer, Python dependencies, the TeleTool systemd service, and the sudo rules
needed for System page network/power controls. It also creates Tvheadend blank
username/blank password local admin access so TeleTool can use the local
Tvheadend API without interactive first-run setup. When run in a terminal it
uses a full-screen stage display and progress bar. Its completion screen shows
the unit's TeleTool Web UI link and the remaining browser-based setup action.
Redirected output automatically uses plain text for clean logs.

By default the bootstrap installs the `dev` branch and seeds the Crystal Palace
DVB-T/T2 scanfile:

```sh
TELETOOL_BRANCH=dev \
TELETOOL_DVBT_SCANFILE=dvb-t/uk/dvb-t_uk-CrystalPalac \
bash scripts/pi_full_setup.sh
```

Useful clean-install options:

- `TELETOOL_PROJECT_DIR=/home/admin/tvh_ndi_bridge` - install location
- `TELETOOL_SERVICE_USER=admin` - systemd service user
- `TELETOOL_DVBT_SCANFILE=<scanfile>` - default TV transmitter/region
- `TELETOOL_TVH_NETWORK_NAME=<name>` - seeded Tvheadend DVB-T network name
- `TELETOOL_TVH_OPEN_LAN=1` - make the blank Tvheadend admin access LAN-wide
- `TELETOOL_NDI_LIB=/path/to/libndi.so.6` - override the NDI runtime drop path
- `TELETOOL_APT_UPGRADE=1` - run `apt full-upgrade` before installing packages
- `TELETOOL_TERMINAL_UI=0` - disable the full-screen terminal presentation

The setup script builds and installs the open-source GStreamer NDI plugin that
provides `ndisink` and `ndisinkcombiner`. The proprietary NDI SDK runtime is not
bundled. Download the NDI SDK for Linux from
[NDI SDK for Developers](https://ndi.video/for-developers/ndi-sdk/), extract the
ARM64 library named `libndi.so.6`, then open the TeleTool link printed on the
installer completion screen and drop the file onto the upload box. TeleTool
installs the runtime, refreshes the dynamic linker cache, verifies the required
NDI elements, and opens the normal UI automatically.

Until `libndi.so.6` is installed, `/`, `/audio`, `/system`, and `/manager`
redirect to `/ndi-setup`. The holding page links to the official SDK, shows the
required filename, and provides a drag-and-drop upload box. TeleTool streams the
upload with a size limit, verifies that it is an ARM64 ELF library exposing the
NDI loader API, installs it through a fixed root-owned helper, refreshes the
dynamic linker cache, and confirms the GStreamer NDI elements. The normal UI
unlocks only after every check succeeds. API endpoints remain available for
diagnostics while the UI is held.

After the script finishes, open TeleTool, confirm the DVB-T/T2 transmitter in TV
Setup, and run the scan. For the Crystal Palace area, use the specific Crystal
Palace region rather than the broad TeleTool UK auto scan to avoid duplicate
services from other transmitters.

## Raspberry Pi Project Setup

TeleTool expects:

- Raspberry Pi OS on a Raspberry Pi 5
- Tvheadend available locally or on the configured URL
- Python 3 with PyGObject
- GStreamer base/good/bad/ugly/libav and ALSA plugins
- The ARM64 NDI SDK 6 runtime file `libndi.so.6`; the setup script builds the
  GStreamer plugin that provides `ndisink`

On the Pi, after syncing the project:

```sh
bash scripts/pi_setup.sh
```

The project setup script is the smaller installer for an already-synced checkout.
It installs common Debian/Raspberry Pi OS packages, creates `.venv` with system
site packages, installs Python requirements, and installs the systemd service.
Use `scripts/pi_full_setup.sh` for a clean Raspberry Pi OS Lite bootstrap.

The smaller project setup does not build the NDI plugin. Use the full bootstrap
for a clean OS installation, including the GStreamer plugin build and the
`libndi.so.6` drop-file workflow. Verify the result with:

```sh
gst-inspect-1.0 ndisink
```

Check the service:

```sh
systemctl status tvh_ndi_bridge.service
journalctl -u tvh_ndi_bridge.service -f
```

## Windows/VS Code Sync

Create `.env.local` for local Pi sync credentials:

```text
TELETOOL_PI_HOST=192.168.0.142
TELETOOL_PI_USER=admin
TELETOOL_PI_PASSWORD=<your-pi-password>
TELETOOL_PI_PATH=/home/admin/tvh_ndi_bridge
```

Do not commit `.env.local`.

Useful VS Code tasks:

- `Pi: Sync project` - copy code and static assets while preserving remote `config.json`
- `Pi: Sync project including config` - copy everything including `config.json`
- `Pi: Setup dependencies/service` - run `scripts/pi_setup.sh`
- `Pi: Restart service` - restart `tvh_ndi_bridge.service`
- `Pi: Tail service logs` - follow service logs over SSH
- `Local: Run API` - run the FastAPI app locally

Normal deployment flow:

1. Run `Pi: Sync project`.
2. Run `Pi: Setup dependencies/service` after a fresh flash or package change.
3. Run `Pi: Restart service`.
4. Open `http://<pi>:8000/`.

## Fleet Manager

Open `/manager` to monitor and control multiple TeleTool units from one primary unit. Add units by IP address or hostname. Each unit reports system status, stream status, version, hostname/IP, NDI source name, and current channel when available.

When a unit is adopted by an active Fleet Manager, its own manager page redirects back to the managing unit until the adoption lease expires.

## Software Updates

The System page can update the program from GitHub branch `main` or `dev`. The updater preserves local runtime files such as `config.json`, `.env.local`, `.venv`, git metadata, and generated image artifacts.

Use `main` for stable releases and `dev` for development builds.

## Golden Images

The optional image builder can create a compact Raspberry Pi SD-card image for cloning:

```sh
sudo env COMPRESS=xz ROOT_MIN_MIB=12288 ROOT_MARGIN_MIB=4096 \
  bash scripts/pi_make_golden_image.sh /home/admin/golden-master teletool-pi5-golden-$(date +%Y%m%d-%H%M%S)
```

Generated images are ignored by git. The builder stops TeleTool and Tvheadend during copy, updates boot identifiers, adds first-boot root expansion, clears clone-specific machine state, and writes checksum/manifest sidecar files.

## Project Layout

- `app.py` - FastAPI web/API entrypoint
- `gst_ndi.py`, `gst_base.py` - GStreamer pipeline control
- `tvh.py` - Tvheadend API client and scan helpers
- `static/` - web UI
- `API.md` - concise JSON API reference
- `config.example.json` - committed config template
- `config.json` - local runtime config, ignored by git
- `deploy/systemd/tvh_ndi_bridge.service` - systemd service file
- `scripts/pi_full_setup.sh` - clean Raspberry Pi OS Lite bootstrap
- `scripts/pi_sync.ps1` - Windows sync helper
- `scripts/pi_setup.sh` - Pi dependency and service setup
- `scripts/pi_make_golden_image.sh` - optional SD-card image builder

## Development Checks

On a development machine:

```powershell
python -m compileall .
```

On the Pi, also check:

```sh
gst-inspect-1.0 ndisink
gst-inspect-1.0 alsasink
systemctl status tvh_ndi_bridge.service
```

Run the app manually on the Pi:

```sh
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

## Troubleshooting

Check TeleTool:

```sh
systemctl status tvh_ndi_bridge.service
journalctl -u tvh_ndi_bridge.service -n 120 --no-pager
```

Check Tvheadend:

```sh
curl http://127.0.0.1:9981/api/serverinfo
systemctl status tvheadend
```

Check audio devices:

```sh
aplay -l
curl http://127.0.0.1:8000/api/audio/devices
```

If `/api/audio/devices` is empty and `aplay -l` only shows HDMI devices, that is expected. Connect a Dante AVIO or other suitable USB audio output and refresh the audio page.

If the web UI cannot change network settings or reboot the Pi, install the helper once:

```sh
sudo ./install_network_privileges.sh
```
