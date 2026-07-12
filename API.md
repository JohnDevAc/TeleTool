# TeleTool API

TeleTool exposes a small JSON API from the same FastAPI service as the web UI.
It controls NDI® output and related Tvheadend and local-audio functions.

Base URL:

```text
http://<teletool-host>:8000
```

FastAPI also publishes generated docs at `/docs` and the OpenAPI schema at `/openapi.json`.

## Notes

- Requests and responses use JSON.
- There is currently no API authentication; expose TeleTool only on a trusted LAN or VPN.
- Errors use normal HTTP status codes and usually return `{"detail": "message"}`.
- Use `channel_uuid` values from `GET /api/channels` when starting NDI.

## Channels And NDI

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/channels?force_refresh=0` | List Tvheadend channels. |
| `GET` | `/api/status?lite=1&stats=1&logs=0&rf=0` | Current NDI, audio, and supervisor status. Set `rf=1` to include RF status in the same response. |
| `GET` | `/api/rf` | Current cached/live Tvheadend RF status. The web UI polls this separately from pipeline status. |
| `GET` | `/api/ndi/runtime` | NDI SDK runtime readiness, paths, SDK URL, and upload availability. |
| `POST` | `/api/ndi/runtime/upload` | Upload, validate, and install an ARM64 `libndi.so.6` request body. |
| `POST` | `/api/start` | Start or restart the NDI stream. |
| `POST` | `/api/stop` | Stop NDI and local audio output. |

Start NDI:

```json
{
  "channel_uuid": "17bd44180657823bdb1cdc7e27b71610",
  "ndi_name": "TeleTool",
  "profile": "pass",
  "deinterlace": false,
  "buffer_extra_ms": 0,
  "ndi_qos": false,
  "ndi_multicast_enabled": false,
  "ndi_multicast_addr": "",
  "ndi_multicast_ttl": 1
}
```

Minimal curl example:

```sh
curl http://teletool.local:8000/api/channels

curl -X POST http://teletool.local:8000/api/start \
  -H "Content-Type: application/json" \
  -d '{"channel_uuid":"<uuid>","ndi_name":"TeleTool","profile":"pass"}'
```

The runtime upload endpoint accepts the library itself as an
`application/octet-stream` request body. It is intended for the `/ndi-setup`
holding page and rejects non-ELF, non-64-bit, non-AArch64, oversized, or
non-NDI files.

## Local Audio Output

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/audio/devices` | List suitable local output devices. |
| `GET` | `/api/audio/defaults` | Current default audio device and volume. |
| `GET` | `/api/audio/status?logs=1` | Local audio output status. |
| `POST` | `/api/audio/start` | Start line output from the active NDI pipeline. |
| `POST` | `/api/audio/stop` | Stop line output. |

Start audio:

```json
{
  "device_id": "alsa:hw:CARD=AVIO,DEV=0",
  "volume": 0.8
}
```

NDI must already be running before audio can start.

## TV Setup

TV setup rebuilds Tvheadend tuner/channel data and should not be run while on air.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/tv/setup/regions` | List DVB-T/T2 scan regions and the selected default. |
| `POST` | `/api/tv/setup/run` | Start a destructive Tvheadend scan and service map. |
| `GET` | `/api/tv/setup/status` | Poll setup progress, logs, and result. |

Run TV setup with the default region:

```json
{}
```

Run TV setup with the TeleTool UK auto scan:

```json
{
  "scanfile": "teletool/uk-auto-dvbt-dvbt2"
}
```

## UI Config

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/config/ui` | Read editable UI/runtime config. |
| `POST` | `/api/config/ui` | Patch editable config values. |

Common keys include:

```json
{
  "tvh_stream_profile": "pass",
  "tvh_dvbt_scanfile": "teletool/uk-auto-dvbt-dvbt2",
  "ndi_default_name": "TeleTool",
  "ndi_delay_ms": 500,
  "ndi_deinterlace": false,
  "ndi_stall_timeout_s": 15.0,
  "lineout_volume": 0.8
}
```

## System

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/release` | App version and release branch. |
| `GET` | `/api/system/hostname` | Current hostname. |
| `POST` | `/api/system/hostname` | Set hostname. |
| `GET` | `/api/system/network_info` | Current network interfaces and warnings. |
| `POST` | `/api/system/network` | Set eth0 DHCP or manual IPv4 config. |
| `POST` | `/api/system/restart_program` | Restart the TeleTool process. |
| `POST` | `/api/system/reboot` | Reboot the Pi, if permitted. |
| `GET` | `/api/system/update_status` | Poll software update state. |
| `POST` | `/api/system/update_from_server` | Update a checkout from GitHub, or switch a package-managed unit to the signed Main/Dev APT channel selected by `branch`. |

Manual network example:

```json
{
  "mode": "manual",
  "ip_address": "192.168.1.50",
  "subnet_mask": "255.255.255.0",
  "gateway": "192.168.1.1",
  "dns": "1.1.1.1 8.8.8.8"
}
```

DHCP example:

```json
{
  "mode": "dhcp"
}
```

Update example:

```json
{
  "confirm": true,
  "branch": "dev"
}
```

## Fleet Manager

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/manager/status` | Combined status for the primary and managed units. |
| `GET` | `/api/manager/units` | List managed units. |
| `POST` | `/api/manager/units` | Add one or more units by host/IP. |
| `DELETE` | `/api/manager/units/{unit_id}` | Remove a managed unit. |
| `POST` | `/api/manager/units/{unit_id}/start` | Start NDI on a managed unit using its last/default request. |
| `POST` | `/api/manager/units/{unit_id}/stop` | Stop NDI on a managed unit. |
| `GET` | `/api/manager/adoption` | Show whether this unit is adopted by a manager. |
| `POST` | `/api/manager/adoption/heartbeat` | Manager adoption lease heartbeat. |
| `POST` | `/api/manager/adoption/release` | Release manager adoption lease. |
| `POST` | `/api/manager/snapshot` | Return status, release, hostname, config, and adoption state in one Fleet Manager request. |

Add units:

```json
{
  "host": "192.168.1.21, teletool-stage.local"
}
```
