# TeleTool

TeleTool is a Raspberry Pi 5 FastAPI app that controls Tvheadend streams, publishes them as NDI, and can route the active stereo audio to a local USB/ALSA line output such as a Dante AVIO USB adapter.

## Project Layout

- `app.py` - FastAPI web/API entrypoint
- `gst_ndi.py`, `gst_base.py` - GStreamer pipeline control
- `tvh.py` - Tvheadend API client
- `static/` - web UI
- `config.example.json` - default config template
- `config.json` - local runtime config used by the app, ignored by git
- `.vscode/tasks.json` - VS Code tasks for syncing and managing the Pi
- `scripts/pi_sync.ps1` - Windows/VS Code sync script
- `scripts/pi_setup.sh` - Raspberry Pi dependency and service setup

## VS Code Raspberry Pi Sync

1. Install the recommended VS Code extensions when prompted:
   - Python
   - Remote - SSH
   - PowerShell

2. Edit `.vscode/settings.json` for your Pi:

   ```json
   {
     "teletool.piHost": "raspberrypi.local",
     "teletool.piUser": "pi",
     "teletool.piPath": "/home/pi/teletool"
   }
   ```

3. Make sure Windows can SSH to the Pi:

   ```powershell
   ssh pi@raspberrypi.local
   ```

4. In VS Code, run `Terminal > Run Task... > Pi: Sync project`.

5. Run `Terminal > Run Task... > Pi: Setup dependencies/service`.

6. Open the app:

   ```text
   http://raspberrypi.local:8000/
   ```

The normal sync task preserves `config.json` on the Pi, because the web UI writes device-specific settings there. Use `Pi: Sync project including config` only when you intentionally want to overwrite the Pi config from this workspace.

## Raspberry Pi Requirements

The setup script installs common Raspberry Pi OS packages for:

- Python virtual environments
- PyGObject
- GStreamer base/good/bad/ugly/libav and ALSA plugins
- ALSA device discovery tools
- Avahi helpers

NDI support still requires an ARM64 NDI runtime/GStreamer plugin that provides `ndisink`. After installing it, this should succeed on the Pi:

```sh
gst-inspect-1.0 ndisink
```

Tvheadend is expected at `http://127.0.0.1:9981` by default. Change `tvh_base_url` in the web UI or `config.json` if Tvheadend is elsewhere.

For Dante AVIO USB output, connect the adapter to the Pi and open `/audio`. The Audio output dropdown only lists the Pi analogue/headphones output and USB audio devices suitable for a Dante AVIO adapter; HDMI and internal ALSA aliases are hidden. Output level defaults to 80% (about -1.9 dB) and is controlled with a slider.

For network settings, the app uses `nmcli` when NetworkManager is already present and falls back to `dhcpcd` style config otherwise.

## Useful VS Code Tasks

- `Pi: Sync project` - copy code and static assets to the Pi while preserving remote `config.json`
- `Pi: Sync project including config` - copy everything including `config.json`
- `Pi: Setup dependencies/service` - install packages, create `.venv`, install systemd service
- `Pi: Restart service` - restart `tvh_ndi_bridge.service`
- `Pi: Tail service logs` - follow service logs over SSH
- `Local: Run API` - run the FastAPI app locally with uvicorn

## Service Commands On The Pi

```sh
sudo systemctl restart tvh_ndi_bridge.service
systemctl status tvh_ndi_bridge.service
journalctl -u tvh_ndi_bridge.service -f
```

## Local Development

On Windows, full local runtime may be limited because PyGObject and GStreamer are easiest on Raspberry Pi OS. Lightweight syntax checks still work:

```powershell
python -m compileall .
```

On the Pi:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
```
