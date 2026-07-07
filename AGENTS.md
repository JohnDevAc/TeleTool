# TeleTool Project Notes

This is a FastAPI app for a Raspberry Pi 5 that bridges Tvheadend streams to NDI and local line-level audio output using GStreamer.

## Runtime

- Entrypoint: `uvicorn app:app --host 0.0.0.0 --port 8000`
- Main app: `app.py`
- GStreamer bridge: `gst_ndi.py`, `gst_base.py`
- Tvheadend client: `tvh.py`
- Static UI: `static/`
- Runtime config: `config.json`

## Raspberry Pi Notes

- The Pi needs system GStreamer packages, PyGObject, an NDI GStreamer sink plugin that provides `ndisink`, and ALSA output support for USB audio devices such as Dante AVIO.
- `scripts/pi_setup.sh` installs common Debian/Raspberry Pi OS packages, creates `.venv` with system site packages, installs Python dependencies, and installs the systemd service.
- `scripts/pi_sync.ps1` is designed for VS Code on Windows. By default it does not overwrite `config.json` on the Pi because the web UI edits that file at runtime.

## Validation

Use lightweight checks unless the requested change touches live GStreamer behavior:

```powershell
python -m compileall .
```

On the Pi, also check:

```sh
gst-inspect-1.0 ndisink
gst-inspect-1.0 alsasink
systemctl status tvh_ndi_bridge.service
journalctl -u tvh_ndi_bridge.service -f
```
