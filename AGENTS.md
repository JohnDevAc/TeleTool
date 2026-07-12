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
- The only supported installation is the signed APT repository bootstrap published at `apt-repo/install.sh` and invoked through the WGET command in `README.md`.
- Runtime updates use the signed Main and Dev APT repositories; checkout-based installation and source ZIP updates are unsupported.

## Validation

Use lightweight checks unless the requested change touches live GStreamer behavior:

```powershell
python -m compileall .
```

On the Pi, also check:

```sh
gst-inspect-1.0 ndisink
gst-inspect-1.0 alsasink
systemctl status teletool.service
journalctl -u teletool.service -f
```
