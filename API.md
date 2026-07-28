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
| `GET` | `/api/rf` | Current cached/live Tvheadend RF status. Calibrated carrier-to-noise is returned as `cnr_db` and `cnr_label`; the web UI polls this separately from pipeline status. |
| `GET` | `/api/ndi/runtime` | NDI SDK runtime readiness, paths, SDK URL, and upload availability. |
| `POST` | `/api/ndi/runtime/upload` | Upload, validate, and install an ARM64 `libndi.so.6` request body. |
| `POST` | `/api/start` | Start or restart the NDI stream. |
| `POST` | `/api/stop` | Stop NDI and local audio output. |

Start NDI:

```json
{
  "channel_uuid": "17bd44180657823bdb1cdc7e27b71610",
  "ndi_name": "TeleTool",
  "ndi_groups": "Public",
  "profile": "pass",
  "deinterlace": false,
  "buffer_extra_ms": 0,
  "ndi_qos": false,
  "ndi_multicast_enabled": false,
  "ndi_multicast_netprefix": "239.255.0.0",
  "ndi_multicast_netmask": "255.255.0.0",
  "ndi_multicast_ttl": 1
}
```

The multicast prefix must be the first address in a contiguous IPv4 multicast
range. Changing NDI groups, Discovery Server, or multicast settings causes a
controlled program restart; the requested stream starts automatically after
the NDI runtime reloads its configuration.

Minimal curl example:

```sh
curl http://teletool.local:8000/api/channels

curl -X POST http://teletool.local:8000/api/start \
  -H "Content-Type: application/json" \
  -d '{"channel_uuid":"<uuid>","ndi_name":"TeleTool","profile":"pass"}'
```

The runtime status response includes `capabilities.sender_advertiser`, which is
true only when the installed runtime exposes the NDI sender-advertiser API used
by the Discovery app's advertising-sender view. The runtime upload endpoint
accepts the library itself as an `application/octet-stream` request body. It is
intended for the `/ndi-setup` holding page and rejects non-ELF, non-64-bit,
non-AArch64, oversized, or non-NDI files.

## Local Audio Output

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/audio/devices` | List suitable local outputs and a separately installed Inferno ALSA network output, including readiness. |
| `GET` | `/api/audio/defaults` | Current default audio device and volume. |
| `GET` | `/api/audio/status?logs=1` | Local audio output status. |
| `POST` | `/api/audio/start` | Start an isolated audio-only output for the active TV channel. |
| `POST` | `/api/audio/stop` | Stop line output. |

Inferno device entries include `ptp_state` and set `ready` to `false` while no
PTP grandmaster or primary leader is available. In that state, `details`
contains a short operator-facing explanation and audio start returns HTTP 409.

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
| `GET` | `/api/tv/setup/report` | Download the most recent TV scan report as a PDF. |

Setup status includes `muxes_scanned`, `muxes_total`, `services_found`,
`report_available`, `report_url`, and `report_error`.
These counters are updated by the existing scan poll and remain available in
complete, partial, and failed results. Starting a new scan removes the previous
report. The replacement PDF records the unit identity, scan result, mux/service
counts, and calibrated average dBm and C/N readings for each mux.

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
  "tvh_dvbt_scanfile": "",
  "ndi_default_name": "TeleTool",
  "ndi_groups": "Public",
  "ndi_discovery_server": "192.168.1.20",
  "ndi_delay_ms": 500,
  "ndi_deinterlace": false,
  "ndi_stall_timeout_s": 15.0,
  "lineout_volume": 0.8
}
```

New installations and an empty `tvh_dvbt_scanfile` select TeleTool's Generic
Auto Default UK profile, which scans UHF channels 21-48 using both DVB-T and
DVB-T2. DVB-T2 is checked at the nominal centre frequency and the UK
`+/-167 kHz` offsets so HD multiplexes are not dependent on tuner AFC behavior.
Existing installations using the legacy Tvheadend Generic profile are migrated
to this default when the tuning page is loaded. The legacy profile is DVB-T
only. A scan that finds standard-definition services but does not lock a DVB-T2
multiplex is reported as partial because HD services may be missing.
After TV setup completes, TeleTool stores the exact scanfile key returned by the
installed Tvheadend version.
`ndi_groups` and `ndi_discovery_server` accept comma-separated values; discovery
server entries must be IP addresses, with optional `:port`.
NDI group and Discovery Server changes are SDK startup settings. If a config
change returns `restart_required: true`, TeleTool will restart before those
settings become active. A `/api/start` request that changes the group may return
`restart_required: true`; the requested stream is saved and started
automatically after the restart.

## System

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/release` | App version, release branch, and Inferno package state. |
| `GET` | `/api/system/hostname` | Current hostname. |
| `POST` | `/api/system/hostname` | Set hostname. |
| `GET` | `/api/system/network_info` | Current network interfaces and warnings. |
| `POST` | `/api/system/network` | Set eth0 DHCP or manual IPv4 config. |
| `POST` | `/api/system/restart_program` | Restart the TeleTool process. |
| `POST` | `/api/system/reboot` | Reboot the Pi, if permitted. |
| `GET` | `/api/system/update_status` | Poll software update state. |
| `POST` | `/api/system/update_from_server` | Switch a package-managed unit to the signed Main/Dev APT channel selected by `branch`, with `inferno_action` set to `keep`, `install`, or `remove`. |

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
  "branch": "dev",
  "inferno_action": "install"
}
```

## Fleet Manager

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/manager/status` | Combined status for the primary and managed units. |
| `GET` | `/api/manager/units` | List managed units. |
| `GET` | `/api/manager/discovery` | Return this unit's read-only discovery identity and adoption/primary state. |
| `POST` | `/api/manager/discovery/scan` | Discover and classify TeleTools on the local network without adopting them. |
| `POST` | `/api/manager/units` | Add one or more units by host/IP. |
| `DELETE` | `/api/manager/units/{unit_id}` | Remove a managed unit. |
| `POST` | `/api/manager/units/{unit_id}/start` | Start NDI on a managed unit using its last/default request. |
| `POST` | `/api/manager/units/{unit_id}/stop` | Stop NDI on a managed unit. |
| `GET` | `/api/manager/adoption` | Show whether this unit is adopted by a manager. |
| `POST` | `/api/manager/adoption/heartbeat` | Manager adoption lease heartbeat. |
| `POST` | `/api/manager/adoption/release` | Release manager adoption lease. |
| `POST` | `/api/manager/snapshot` | Return status, release, hostname, config, and adoption state in one Fleet Manager request. |

Network discovery uses the `_teletool._tcp` mDNS service and a bounded local
IPv4 scan for compatibility with older units. Results indicate whether each
unit is available, already listed, adopted by another primary, or is itself a
primary managing other units. Adoption happens only after a user selects a
result and posts it to `/api/manager/units`.

Add units:

```json
{
  "host": "192.168.1.21, teletool-stage.local"
}
```
