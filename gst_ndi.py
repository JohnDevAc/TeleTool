import base64
import html
import socket
import tempfile
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, List, Optional
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from inferno_status import inferno_clock_status
from ndi_runtime_config import write_ndi_runtime_config

_DEFAULT_CONFIG_PATH = Path(
    os.environ.get("TELETOOL_CONFIG_PATH", str(Path(__file__).resolve().parent / "config.json"))
).expanduser()

def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load config.json for pipeline defaults.

    The FastAPI app also loads config.json; passing that dict into GstNDIBridge()
    avoids double-reading, but GstNDIBridge can also run standalone.
    """
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _gst_quote(value: Any) -> str:
    """Quote a string for use as a GStreamer parse-launch property value."""
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _test_card_motion_geometry(width: int, height: int) -> tuple[int, int, int]:
    scale = min(width / 1920.0, height / 1080.0)
    size = max(220, int(round(440 * scale)))
    x = max(0, int(round((width - size) / 2)))
    y = max(0, int(round(210 * height / 1080.0)))
    return x, y, size


def _primary_ipv4_address() -> str:
    """Return the primary non-loopback IPv4 address without sending network traffic."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = str(probe.getsockname()[0] or "").strip()
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass

    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            address = str(address or "").strip()
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    return "IP unavailable"


def _test_card_background_svg(
    width: int,
    height: int,
    hostname: str,
    ip_address: str,
    logo_path: Path,
    fps: int = 60,
    tone_hz: int = 1000,
    tone_interval_ms: int = 1000,
    tone_duration_ms: int = 100,
    multicast_enabled: bool = False,
) -> str:
    """Build the static part of the test card; imagefreeze reuses this frame at 60p."""
    try:
        logo_data = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    except Exception:
        logo_data = ""
    logo = (
        '<image x="42" y="17" width="158" height="100" preserveAspectRatio="xMidYMid meet" '
        f'href="data:image/png;base64,{logo_data}"/>'
        if logo_data
        else ""
    )

    bar_colors = ("#bfbfbf", "#bfbf00", "#00bfbf", "#00bf00", "#bf00bf", "#bf0000", "#0000bf")
    bar_width = 1840.0 / len(bar_colors)
    bars = "".join(
        f'<rect x="{40 + index * bar_width:.2f}" y="132" width="{bar_width + 0.5:.2f}" height="558" fill="{color}"/>'
        for index, color in enumerate(bar_colors)
    )
    reverse_bars = "".join(
        f'<rect x="{40 + index * bar_width:.2f}" y="690" width="{bar_width + 0.5:.2f}" height="78" fill="{color}"/>'
        for index, color in enumerate(reversed(bar_colors))
    )
    grey_values = (16, 32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 240)
    grey_width = 720.0 / len(grey_values)
    greys = "".join(
        f'<rect x="600" y="816" width="{grey_width + 0.5:.2f}" height="92" '
        f'fill="rgb({value},{value},{value})" transform="translate({index * grey_width:.2f},0)"/>'
        for index, value in enumerate(grey_values)
    )
    frame_ticks = "".join(
        f'<line x1="960" y1="236" x2="960" y2="{252 if frame % 5 == 0 else 244}" '
        f'transform="rotate({frame * 6} 960 430)" '
        f'class="clockTick{" clockTickMajor" if frame % 10 == 0 else ""}"/>'
        for frame in range(60)
    )
    frame_spokes = "".join(
        f'<line x1="960" y1="430" x2="960" y2="236" '
        f'transform="rotate({frame * 6} 960 430)" class="frameSpoke"/>'
        for frame in range(10, 60, 10)
    )
    frame_labels = "".join(
        f'<text x="{x}" y="{y}" text-anchor="middle" class="frameLabel">{label}</text>'
        for x, y, label in (
            (960, 230, "0"),
            (1142, 332, "+10"),
            (1142, 538, "+20"),
            (960, 640, "30"),
            (778, 538, "-20"),
            (778, 332, "-10"),
        )
    )
    multicast_indicator = (
        '''<g aria-label="Multicast Send">
    <title>Multicast Send</title>
    <path d="M58 1008 Q46 1020 58 1032 M48 998 Q30 1020 48 1042
             M82 1008 Q94 1020 82 1032 M92 998 Q110 1020 92 1042"
          fill="none" stroke="#39e58c" stroke-width="4" stroke-linecap="round"/>
    <circle cx="70" cy="1020" r="7" fill="#39e58c"/>
  </g>'''
        if multicast_enabled
        else ""
    )

    hostname_i = html.escape(str(hostname or "TeleTool"), quote=True)
    ip_address_i = html.escape(str(ip_address or "IP unavailable"), quote=True)
    format_i = f"{height}p{fps}" if width * 9 == height * 16 else f"{width} x {height}p{fps}"
    interval_label = "EVERY SECOND" if tone_interval_ms == 1000 else f"EVERY {tone_interval_ms} ms"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 1920 1080">
  <style>
    text {{ font-family: "DejaVu Sans", sans-serif; fill: #f5f7fa; }}
    .eyebrow {{ font-size: 18px; font-weight: 700; fill: #9fb7cc; letter-spacing: 2px; }}
    .title {{ font-size: 36px; font-weight: 700; }}
    .host {{ font-size: 25px; font-weight: 600; fill: #ffd400; }}
    .small {{ font-size: 17px; font-weight: 600; }}
    .tiny {{ font-size: 14px; font-weight: 700; fill: #b8c5d0; letter-spacing: 1px; }}
    .clockTick {{ stroke: #dce5ec; stroke-width: 2; stroke-linecap: round; }}
    .clockTickMajor {{ stroke-width: 4; }}
    .frameSpoke {{ stroke: #657987; stroke-width: 2.5; }}
    .frameLabel {{ font-size: 16px; font-weight: 700; paint-order: stroke; stroke: #0b1117; stroke-width: 4; }}
  </style>
  <rect width="1920" height="1080" fill="#080c10"/>
  <rect x="0" y="0" width="1920" height="132" fill="#101820"/>
  {logo}
  <text x="960" y="54" text-anchor="middle" class="eyebrow">TELETOOL TEST SIGNAL</text>
  <text x="960" y="96" text-anchor="middle" class="host">{ip_address_i}</text>
  <text x="1868" y="65" text-anchor="end" class="title">{format_i}</text>
  <text x="1868" y="101" text-anchor="end" class="small">PROGRESSIVE | NDI</text>
  {bars}
  {reverse_bars}
  <rect x="40" y="786" width="520" height="156" fill="#101820" stroke="#506170" stroke-width="2"/>
  <text x="66" y="816" class="tiny">PLUGE / BLACK LEVEL</text>
  <rect x="66" y="842" width="130" height="72" fill="#050505"/>
  <rect x="196" y="842" width="130" height="72" fill="#101010"/>
  <rect x="326" y="842" width="130" height="72" fill="#1b1b1b"/>
  <text x="131" y="932" text-anchor="middle" class="tiny">-2%</text>
  <text x="261" y="932" text-anchor="middle" class="tiny">0%</text>
  <text x="391" y="932" text-anchor="middle" class="tiny">+2%</text>
  <rect x="580" y="786" width="760" height="156" fill="#101820" stroke="#506170" stroke-width="2"/>
  <text x="600" y="816" class="tiny">LUMINANCE RAMP</text>
  {greys}
  <rect x="1360" y="786" width="520" height="156" fill="#101820" stroke="#506170" stroke-width="2"/>
  <text x="1384" y="816" class="tiny">ALIGNMENT PULSE</text>
  <text x="1384" y="868" class="title">{tone_hz / 1000:g} kHz</text>
  <text x="1384" y="906" class="small">{tone_duration_ms} ms {interval_label}</text>
  <rect x="740" y="210" width="440" height="440" rx="8" fill="#0b1117" fill-opacity="0.96" stroke="#dce5ec" stroke-width="3"/>
  <circle cx="960" cy="430" r="194" fill="none" stroke="#526473" stroke-width="2"/>
  <circle cx="960" cy="430" r="150" fill="none" stroke="#293743" stroke-width="2"/>
  {frame_ticks}
  {frame_spokes}
  <line x1="960" y1="236" x2="960" y2="430" stroke="#ffd400" stroke-width="4"/>
  {frame_labels}
  <circle cx="960" cy="430" r="8" fill="#ffd400"/>
  <text x="960" y="510" text-anchor="middle" class="tiny">MOTION REFERENCE</text>
  <text x="960" y="552" text-anchor="middle" class="small">1 REVOLUTION / SECOND</text>
  <rect x="0" y="960" width="1920" height="120" fill="#101820"/>
  {multicast_indicator}
  <text x="960" y="1021" text-anchor="middle" class="title">{hostname_i}</text>
  <text x="1880" y="1002" text-anchor="end" class="tiny">AUDIO / MOTION SYNC</text>
  <text x="1880" y="1042" text-anchor="end" class="small">1 SECOND CADENCE</text>
  <rect x="25" y="25" width="1870" height="1030" fill="none" stroke="#e8eef3" stroke-width="2" stroke-dasharray="12 12" opacity="0.42"/>
  <rect x="0.5" y="0.5" width="1919" height="1079" fill="none" stroke="#ffffff" stroke-width="1" shape-rendering="crispEdges"/>
</svg>'''


def _write_test_card_background(
    width: int,
    height: int,
    hostname: str,
    ip_address: str,
    fps: int,
    tone_hz: int,
    tone_interval_ms: int,
    tone_duration_ms: int,
    multicast_enabled: bool,
) -> Path:
    logo_path = Path(__file__).resolve().parent / "static" / "teletool-logo.png"
    svg = _test_card_background_svg(
        width,
        height,
        hostname,
        ip_address,
        logo_path,
        fps=fps,
        tone_hz=tone_hz,
        tone_interval_ms=tone_interval_ms,
        tone_duration_ms=tone_duration_ms,
        multicast_enabled=multicast_enabled,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="teletool-test-card-",
        suffix=".svg",
        delete=False,
    ) as stream:
        stream.write(svg)
        return Path(stream.name)


def _write_test_card_marker() -> Path:
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <circle cx="24" cy="24" r="18" fill="#ffd400" stroke="#ffffff" stroke-width="3"/>
  <circle cx="24" cy="24" r="6" fill="#101820"/>
</svg>'''
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="teletool-test-card-marker-",
        suffix=".svg",
        delete=False,
    ) as stream:
        stream.write(svg)
        return Path(stream.name)


from gst_base import GstPipelineBase

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import Gst, GstController  # type: ignore


@dataclass
class RunState:
    running: bool
    pid: Optional[int]
    channel_uuid: Optional[str]
    ndi_name: Optional[str]
    input_url: Optional[str]
    source_mode: Optional[str]
    started_at: Optional[float]
    last_log: List[str]

    pipeline_state: str
    video_caps: Optional[str]
    audio_caps: Optional[str]

    # configured NDI delay (applied as buffering in the pipeline)
    ndi_delay_ms: Optional[int]
    ndi_groups: Optional[str]
    ndi_discovery_server: Optional[str]
    ndi_multicast_enabled: bool
    ndi_multicast_netprefix: Optional[str]
    ndi_multicast_netmask: Optional[str]
    ndi_multicast_ttl: Optional[int]

    # qos/drops
    dropped: int
    qos_events: int

    # ndisink stats
    ndi_rendered: int
    ndi_dropped: int
    ndi_average_rate: float
    ndi_stats_available: bool
    ndi_last_stats_at: Optional[float]

    # estimated fps from rendered deltas
    ndi_fps_est: Optional[float]

    last_error: Optional[str]
    last_warning: Optional[str]
    bitrate_bps_est: Optional[int]


@dataclass
class LineOutRunState:
    running: bool
    device_id: Optional[str]
    device_label: Optional[str]
    sink: Optional[str]
    channel_uuid: Optional[str]
    input_url: Optional[str]
    started_at: Optional[float]
    last_log: List[str]
    pipeline_state: str
    last_error: Optional[str]
    volume: Optional[float]
    sink_sync: Optional[bool]


class GstNDIBridge(GstPipelineBase):
    """Tvheadend stream -> NDI pipeline manager."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, config_path: Optional[str] = None):
        self._cfg: Dict[str, Any] = dict(config or load_config(config_path))

        super().__init__(log_maxlen=int(self._cfg.get("log_maxlen", 400)))
        self._lineout_pipeline = GstPipelineBase(log_maxlen=300)

        # Config-driven feature toggles / defaults (may be overridden per-start).
        self._bitrate_probe_enabled: bool = bool(self._cfg.get("enable_bitrate_probe", False))
        self._bitrate_probe_hooked: bool = False
        self._bitrate_probe_bytes: int = 0
        self._bitrate_probe_last_t: Optional[float] = None

        self._ndi_name: Optional[str] = None
        self._channel_uuid: Optional[str] = None
        self._input_url: Optional[str] = None
        self._source_mode: Optional[str] = None
        self._started_at: Optional[float] = None

        self._video_caps: Optional[str] = None
        self._audio_caps: Optional[str] = None

        # Configured NDI delay (ms) for the currently running pipeline
        self._ndi_delay_ms: Optional[int] = None
        self._ndi_groups: Optional[str] = None
        self._ndi_discovery_server: Optional[str] = None

        # NDI multicast settings used by the running sender.
        self._ndi_multicast_enabled: bool = False
        self._ndi_multicast_netprefix: Optional[str] = None
        self._ndi_multicast_netmask: Optional[str] = None
        self._ndi_multicast_ttl: Optional[int] = None

        self._qos_events: int = 0
        # Estimated bitrate (bps) of buffers reaching NDI (optional; controlled via enable_bitrate_probe).
        self._bitrate_bps_est: Optional[int] = None

        # ndisink stats
        self._ndi_rendered: int = 0
        self._ndi_dropped: int = 0
        self._ndi_average_rate: float = 0.0
        self._ndi_stats_available: bool = False
        self._ndi_last_stats_at: Optional[float] = None
        self._dropped: int = 0  # mapped to ndisink dropped for UI

        # fps estimate from rendered deltas
        self._ndi_fps_est: Optional[float] = None
        self._fps_last_rendered: Optional[int] = None
        self._fps_last_t: Optional[float] = None

        # Line output branch control (lives in the same pipeline; inactive until valve opened)
        self._lineout_enabled: bool = False
        self._lineout_device_id: Optional[str] = None
        self._lineout_device_label: Optional[str] = None
        self._lineout_sink_factory: Optional[str] = None
        self._lineout_volume: float = float(self._cfg.get("lineout_volume", 0.8))
        self._lineout_sink_sync: bool = bool(self._cfg.get("lineout_sink_sync", True))
        self._lineout_started_at: Optional[float] = None
        self._lineout_last_error: Optional[str] = None
        self._lineout_log_full: Deque[str] = deque(maxlen=300)
        # Tail log used for frequent UI polling.
        self._lineout_log_tail: Deque[str] = deque(maxlen=120)
        self._test_card_background_path: Optional[Path] = None
        self._test_card_marker_path: Optional[Path] = None
        self._test_card_controls: List[Any] = []

        # Cached pipeline elements/pads used by the 1 Hz stats poller.
        self._stats_cache_valid: bool = False
        self._stats_ident = None
        self._stats_combiner = None
        self._stats_vpad = None
        self._stats_apad = None
        self._stats_ndisink = None
        self._stats_caps_last_t: float = 0.0

    def update_config(self, config: Dict[str, Any]) -> None:
        """Replace runtime defaults used by future starts/line-output operations."""
        with self._lock:
            self._cfg = dict(config or {})

    def _lineout_log_push(self, msg: str):
        with self._lock:
            line = f"{time.strftime('%H:%M:%S')} {msg}"
            self._lineout_log_full.append(line)
            self._lineout_log_tail.append(line)

    def _setup_test_card_motion(
        self,
        center_x: int,
        center_y: int,
        radius: int,
        marker_size: int,
    ) -> None:
        with self._lock:
            pipeline = self._pipeline
        if pipeline is None:
            raise RuntimeError("Test-card pipeline is not available")
        compositor = pipeline.get_by_name("testcardcompositor")
        pad = compositor.get_static_pad("sink_1") if compositor is not None else None
        if pad is None:
            raise RuntimeError("Test-card motion pad is unavailable")

        controls: List[Any] = []
        position_offset = marker_size // 2
        for property_name, timeshift, offset in (
            ("xpos", 0, center_x - position_offset),
            ("ypos", 250_000_000, center_y - position_offset),
        ):
            property_spec = pad.find_property(property_name)
            if property_spec is None:
                raise RuntimeError(f"Test-card motion property {property_name} is unavailable")
            property_range = float(property_spec.maximum - property_spec.minimum)
            normalized_amplitude = float(radius) / property_range
            normalized_offset = float(offset - property_spec.minimum) / property_range
            source = GstController.LFOControlSource()
            source.set_property("waveform", GstController.LFOWaveform.SINE)
            source.set_property("frequency", 1.0)
            source.set_property("timeshift", timeshift)
            source.set_property("amplitude", normalized_amplitude)
            source.set_property("offset", normalized_offset)
            binding = GstController.DirectControlBinding.new(pad, property_name, source)
            if binding is None or not pad.add_control_binding(binding):
                raise RuntimeError(f"Could not animate test-card {property_name}")
            controls.extend((source, binding))

        with self._lock:
            self._test_card_controls = controls

    @staticmethod
    def _safe_run(argv: List[str], timeout_s: float = 2.0) -> Dict[str, Any]:
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            return {"ok": proc.returncode == 0, "rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except Exception as e:
            return {"ok": False, "rc": None, "stdout": "", "stderr": str(e)}

    @staticmethod
    def _select_lineout_sink_factory() -> str:
        if Gst.ElementFactory.find("alsasink") is not None:
            return "alsasink"
        if Gst.ElementFactory.find("autoaudiosink") is not None:
            return "autoaudiosink"
        if Gst.ElementFactory.find("pulsesink") is not None:
            return "pulsesink"
        return "fakesink"

    def _inferno_clock_status(self, *, force: bool = False) -> Dict[str, Any]:
        clock_path = str(
            self._cfg.get(
                "inferno_clock_path",
                "/run/teletool-inferno/usrvclock.sock",
            )
            or "/run/teletool-inferno/usrvclock.sock"
        )
        observation_path = str(
            self._cfg.get(
                "inferno_clock_observation_path",
                "/run/teletool-inferno/observe.sock",
            )
            or "/run/teletool-inferno/observe.sock"
        )
        return inferno_clock_status(
            clock_path,
            observation_path,
            timeout_s=0.2,
            max_age_s=1.0,
            force=force,
        )

    def audio_output_devices(self) -> List[Dict[str, Any]]:
        """Detect supported local outputs and the optional Inferno ALSA PCM."""
        sink_factory = self._select_lineout_sink_factory()
        devices: List[Dict[str, Any]] = []
        seen = set()
        inferno_device = str(self._cfg.get("inferno_alsa_device", "teletool_inferno") or "").strip()
        inferno_status = self._inferno_clock_status()

        def add(device_id: str, label: str, *, device: Optional[str], sink: str, kind: str, details: str = "") -> None:
            if device_id in seen:
                return
            seen.add(device_id)
            ready = kind != "inferno" or bool(inferno_status.get("ready"))
            if kind == "inferno" and not ready:
                details = str(inferno_status.get("details") or "Inferno PTP clock is not synchronized.")
            devices.append(
                {
                    "id": device_id,
                    "label": label,
                    "sink": sink,
                    "device": device,
                    "kind": kind,
                    "details": details,
                    "ready": ready,
                    "network_output": kind == "inferno",
                    "requires_clock_service": kind == "inferno",
                    "sample_format": "S32LE" if kind == "inferno" else None,
                    "ptp_state": inferno_status.get("state") if kind == "inferno" else None,
                }
            )

        def clean(value: str) -> str:
            return re.sub(r"\s+", " ", str(value or "").replace("_", " ")).strip()

        def classify(text: str) -> Optional[str]:
            haystack = text.lower()
            if "inferno" in haystack:
                return "inferno"
            if any(x in haystack for x in ("hdmi", "vc4hdmi", "vc4-hdmi", "displayport")):
                return None
            if any(x in haystack for x in ("dante", "avio", "audinate")):
                return "avio"
            if "usb" in haystack and any(x in haystack for x in ("audio", "sound", "dac", "device")):
                return "usb"
            if any(x in haystack for x in ("headphone", "headphones", "analogue", "analog", "bcm2835")):
                return "analog"
            return None

        def label_for(kind: str, card_name: str, dev_name: str) -> str:
            if kind == "inferno":
                return "Inferno network audio output"
            if kind == "analog":
                return "HW analogue 3.5mm jack"
            if kind == "avio":
                return "Dante AVIO USB"
            descriptor = clean(card_name) or clean(dev_name)
            return f"USB audio output ({descriptor})" if descriptor else "USB audio output"

        if sink_factory == "alsasink":
            aplay = shutil.which("aplay")
            if aplay:
                res = self._safe_run([aplay, "-l"], timeout_s=2.0)
                if res.get("ok"):
                    for line in str(res.get("stdout") or "").splitlines():
                        m = re.match(r"card\s+(\d+):\s+([^\[]+)\[([^\]]+)\],\s+device\s+(\d+):\s+([^\[]+)\[([^\]]+)\]", line.strip())
                        if not m:
                            continue
                        card_idx, card_short, card_name, dev_idx, dev_short, dev_name = m.groups()
                        detail_text = " ".join(clean(x) for x in (card_short, card_name, dev_short, dev_name))
                        kind = classify(detail_text)
                        if not kind:
                            continue
                        device = f"plughw:{card_idx},{dev_idx}"
                        label = label_for(kind, card_name, dev_name)
                        details = f"{clean(card_name)} - {clean(dev_name)} (ALSA card {card_idx}, device {dev_idx})"
                        add(f"alsa:{device}", label, device=device, sink="alsasink", kind=kind, details=details)

                # Named virtual PCMs such as Inferno are not included in
                # `aplay -l`, so always inspect `aplay -L` as well.
                res = self._safe_run([aplay, "-L"], timeout_s=2.0)
                if res.get("ok"):
                    current: Optional[str] = None
                    desc: List[str] = []

                    def flush() -> None:
                        if not current:
                            return
                        name = current.strip()
                        if not name or name.startswith("null") or name == "default":
                            return
                        detail = " ".join(x for x in desc if x).strip()
                        kind = classify(f"{name} {detail}")
                        is_configured_inferno = bool(inferno_device and name == inferno_device)
                        if is_configured_inferno:
                            kind = "inferno"
                        if not kind:
                            return
                        if kind != "inferno" and not name.startswith(("sysdefault:", "plughw:", "front:")):
                            return
                        label = label_for(kind, detail, name)
                        if kind == "inferno" and not detail:
                            detail = f"Inferno ALSA network output ({name})"
                        add(f"alsa:{name}", label, device=name, sink="alsasink", kind=kind, details=detail or name)

                    for raw in str(res.get("stdout") or "").splitlines():
                        if raw and not raw[0].isspace():
                            flush()
                            current = raw.strip()
                            desc = []
                        elif current:
                            desc.append(raw.strip())
                    flush()
        devices.sort(
            key=lambda d: (
                {"avio": 0, "usb": 0, "analog": 1, "inferno": 2}.get(str(d.get("kind") or ""), 9),
                str(d.get("label") or d.get("id") or "").lower(),
            )
        )
        return devices

    def _resolve_audio_output_device(self, device_id: Optional[str]) -> Dict[str, Any]:
        devices = self.audio_output_devices()
        wanted = str(device_id or "").strip()
        if not wanted:
            wanted = devices[0]["id"] if devices else ""
        for dev in devices:
            if dev.get("id") == wanted:
                if dev.get("ready") is False:
                    raise ValueError(str(dev.get("details") or "Selected audio output is not ready"))
                return dev
        raise ValueError("Selected audio output is not available")
    # ---------- Public API ----------

    def status(self) -> Dict:
        base = self._base_status_fields(include_log=True)
        with self._lock:
            st = RunState(
                running=bool(base["running"]),
                pid=None,
                channel_uuid=self._channel_uuid if base["running"] else None,
                ndi_name=self._ndi_name if base["running"] else None,
                input_url=self._input_url if base["running"] else None,
                source_mode=self._source_mode if base["running"] else None,
                started_at=self._started_at if base["running"] else None,
                last_log=base["last_log"],
                pipeline_state=base["pipeline_state"],
                video_caps=self._video_caps,
                audio_caps=self._audio_caps,
                ndi_delay_ms=self._ndi_delay_ms,
                ndi_groups=self._ndi_groups if base["running"] else None,
                ndi_discovery_server=self._ndi_discovery_server if base["running"] else None,
                ndi_multicast_enabled=bool(self._ndi_multicast_enabled) if base["running"] else False,
                ndi_multicast_netprefix=self._ndi_multicast_netprefix if base["running"] else None,
                ndi_multicast_netmask=self._ndi_multicast_netmask if base["running"] else None,
                ndi_multicast_ttl=self._ndi_multicast_ttl if base["running"] else None,
                dropped=self._dropped,
                qos_events=self._qos_events,
                ndi_rendered=self._ndi_rendered,
                ndi_dropped=self._ndi_dropped,
                ndi_average_rate=self._ndi_average_rate,
                ndi_stats_available=self._ndi_stats_available,
                ndi_last_stats_at=self._ndi_last_stats_at,
                ndi_fps_est=self._ndi_fps_est,
                last_error=base["last_error"],
                last_warning=base["last_warning"],
                bitrate_bps_est=self._bitrate_bps_est,
            )
            return asdict(st)

    
    def status_lite(self, include_logs: bool = False, include_stats: bool = False) -> Dict:
        """Lightweight status for frequent UI polling.

        By default, omits logs and detailed stats to reduce allocations/JSON size.
        """
        base = self._base_status_fields(include_log=bool(include_logs))
        with self._lock:
            running = bool(base.get("running"))
            d: Dict = {
                "running": running,
                "pipeline_state": base.get("pipeline_state"),
                "last_error": base.get("last_error"),
                "last_warning": base.get("last_warning"),
                "channel_uuid": self._channel_uuid if running else None,
                "ndi_name": self._ndi_name if running else None,
                "input_url": self._input_url if running else None,
                "source_mode": self._source_mode if running else None,
                "started_at": self._started_at if running else None,
                "ndi_groups": self._ndi_groups if running else None,
                "ndi_discovery_server": self._ndi_discovery_server if running else None,
                "ndi_multicast_enabled": bool(self._ndi_multicast_enabled) if running else False,
                "ndi_multicast_netprefix": self._ndi_multicast_netprefix if running else None,
                "ndi_multicast_netmask": self._ndi_multicast_netmask if running else None,
                "ndi_multicast_addr": self._ndi_multicast_netprefix if running else None,
                "ndi_multicast_ttl": self._ndi_multicast_ttl if running else None,
            }
            if include_logs:
                d["last_log"] = base.get("last_log", [])
            if include_stats:
                d.update(
                    {
                        "video_caps": self._video_caps,
                        "audio_caps": self._audio_caps,
                        "ndi_delay_ms": self._ndi_delay_ms,
                        "dropped": self._dropped,
                        "qos_events": self._qos_events,
                        "ndi_rendered": self._ndi_rendered,
                        "ndi_dropped": self._ndi_dropped,
                        "ndi_average_rate": self._ndi_average_rate,
                        "ndi_stats_available": self._ndi_stats_available,
                        "ndi_last_stats_at": self._ndi_last_stats_at,
                        "ndi_fps_est": self._ndi_fps_est,
                        "bitrate_bps_est": self._bitrate_bps_est,
                    }
                )
            return d

    def lineout_status(self, include_logs: bool = True) -> Dict:
        ndi_base = self._base_status_fields(include_log=False)
        audio_base = self._lineout_pipeline._base_status_fields(include_log=include_logs)
        with self._lock:
            pipeline_error = audio_base.get("last_error")
            if pipeline_error and self._lineout_enabled:
                self._lineout_enabled = False
                self._lineout_device_id = None
                self._lineout_device_label = None
                self._lineout_sink_factory = None
                self._lineout_started_at = None
                self._lineout_last_error = str(pipeline_error)
            enabled = bool(self._lineout_enabled)
            running = bool(ndi_base["running"]) and bool(audio_base["running"]) and enabled
            logs = list(self._lineout_log_tail) if include_logs else []
            if include_logs:
                logs.extend(audio_base.get("last_log") or [])
            st = LineOutRunState(
                running=running,
                device_id=self._lineout_device_id if enabled else None,
                device_label=self._lineout_device_label if enabled else None,
                sink=self._lineout_sink_factory if enabled else None,
                channel_uuid=self._channel_uuid if ndi_base["running"] else None,
                input_url=self._input_url if ndi_base["running"] else None,
                started_at=self._lineout_started_at if enabled else None,
                last_log=logs[-120:],
                pipeline_state=audio_base["pipeline_state"],
                last_error=self._lineout_last_error or pipeline_error,
                volume=float(self._lineout_volume) if enabled else None,
                sink_sync=bool(self._lineout_sink_sync) if enabled else None,
            )
            d = asdict(st)
            d["ndi_running"] = bool(ndi_base["running"])
            d["ndi_source_mode"] = self._source_mode if ndi_base["running"] else None
            return d

    def _build_lineout_pipeline_desc(
        self,
        input_url: str,
        selected: Dict[str, Any],
        volume: float,
        sink_sync: bool,
        source_mode: str = "tv",
    ) -> str:
        sink_factory = str(selected.get("sink") or "")
        selected_kind = str(selected.get("kind") or "")
        rate_hz = int(self._cfg.get("audio_rate_hz", 48000))
        channels = int(self._cfg.get("audio_channels", 2))
        queue_time_ns = max(50, int(self._cfg.get("lineout_queue_time_ms", 200))) * 1_000_000

        if sink_factory == "alsasink":
            device = str(selected.get("device") or "").strip()
            if not device:
                raise RuntimeError("Selected ALSA output has no device name")
            inferno_props = ""
            if selected_kind == "inferno":
                buffer_time_us = max(21333, int(self._cfg.get("inferno_alsa_buffer_time_us", 85333)))
                latency_time_us = max(1333, int(self._cfg.get("inferno_alsa_latency_time_us", 5333)))
                inferno_props = (
                    f"provide-clock=false slave-method=resample "
                    f"buffer-time={buffer_time_us} latency-time={latency_time_us} "
                )
            sink = (
                f"alsasink name=lineoutsink device={_gst_quote(device)} {inferno_props}"
                f'async=false sync={"true" if sink_sync else "false"}'
            )
        elif sink_factory in {"autoaudiosink", "pulsesink"}:
            sink = (
                f'{sink_factory} name=lineoutsink async=false '
                f'sync={"true" if sink_sync else "false"}'
            )
        else:
            raise RuntimeError(selected.get("details") or "No usable audio output sink found")

        output_caps = (
            f"audio/x-raw,format=S32LE,rate={rate_hz},channels={channels},layout=interleaved"
            if selected_kind == "inferno"
            else f"audio/x-raw,rate={rate_hz},channels={channels},layout=interleaved"
        )
        if source_mode == "test_card":
            if Gst.ElementFactory.find("interaudiosrc") is None:
                raise RuntimeError("Test-card alignment tone routing is unavailable")
            source = (
                "interaudiosrc channel=teletool-test-card buffer-time=100000000 "
                "latency-time=50000000 period-time=10000000 "
                f"! queue max-size-buffers=0 max-size-bytes=0 max-size-time={queue_time_ns} "
            )
        else:
            decoder = "uridecodebin3" if Gst.ElementFactory.find("uridecodebin3") is not None else "uridecodebin"
            decoder_props = (
                f"{decoder} uri={_gst_quote(input_url)} name=lineoutdecode caps=audio/x-raw"
            )
            if decoder == "uridecodebin":
                decoder_props += " expose-all-streams=false"
            source = (
                f"{decoder_props} lineoutdecode. ! queue max-size-buffers=0 "
                f"max-size-bytes=0 max-size-time={queue_time_ns} "
            )
        return (
            f"{source}"
            f"! audioconvert ! audioresample "
            f"! capsfilter caps={_gst_quote(output_caps)} "
            f"! volume volume={volume:.3f} ! {sink}"
        )

    def lineout_start(self, device_id: Optional[str] = None, volume: Optional[float] = None):
        """Start an isolated audio pipeline for TV audio or the test-card pulse."""
        with self._lock:
            self._lineout_last_error = None
            source_mode = self._source_mode
        base = self._base_status_fields(include_log=False)
        if not base.get("running"):
            raise RuntimeError("NDI pipeline must be running before line output can be started")
        if source_mode not in {"tv", "test_card"}:
            raise RuntimeError("The active NDI source does not provide audio output")

        try:
            selected = self._resolve_audio_output_device(device_id or self._cfg.get("lineout_default_device"))
        except ValueError as e:
            with self._lock:
                self._lineout_last_error = str(e)
            raise
        sink_factory = str(selected.get("sink") or "")
        selected_kind = str(selected.get("kind") or "")
        if selected_kind == "inferno":
            ptp_status = self._inferno_clock_status(force=True)
            if not ptp_status.get("ready"):
                error = str(ptp_status.get("details") or "Inferno PTP clock is not synchronized.")
                with self._lock:
                    self._lineout_last_error = error
                raise ValueError(error)

        try:
            volume_i = float(self._cfg.get("lineout_volume", 0.8) if volume is None else volume)
        except Exception:
            volume_i = 0.8
        volume_i = max(0.0, min(1.0, volume_i))
        sink_sync = bool(self._cfg.get("lineout_sink_sync", True))
        with self._lock:
            input_url = str(self._input_url or "")
        if source_mode == "tv" and not input_url:
            raise RuntimeError("Active TV stream URL is not available")

        try:
            pipeline_desc = self._build_lineout_pipeline_desc(
                input_url=input_url,
                selected=selected,
                volume=volume_i,
                sink_sync=sink_sync,
                source_mode=str(source_mode),
            )
            self._lineout_pipeline._start_pipeline(pipeline_desc)
            self._lineout_pipeline._wait_until_playing(
                timeout_s=10.0 if selected_kind == "inferno" else 5.0
            )
        except Exception as e:
            self._lineout_pipeline.stop()
            error = str(e)
            if selected_kind == "inferno":
                ptp_status = self._inferno_clock_status(force=True)
                if not ptp_status.get("ready"):
                    error = str(ptp_status.get("details") or error)
                else:
                    error = (
                        "Inferno audio could not synchronize with the PTP primary leader. "
                        "Check the audio network and try again."
                    )
            with self._lock:
                self._lineout_last_error = error
            if selected_kind == "inferno":
                raise ValueError(error) from e
            raise

        with self._lock:
            self._lineout_enabled = True
            self._lineout_device_id = str(selected.get("id") or "")
            self._lineout_device_label = str(selected.get("label") or selected.get("id") or "")
            self._lineout_sink_factory = sink_factory
            self._lineout_volume = volume_i
            self._lineout_sink_sync = sink_sync
            self._lineout_started_at = time.time()
            self._lineout_last_error = None
        content = "test-card alignment pulse" if source_mode == "test_card" else "TV audio"
        self._lineout_log_push(
            f"Line output enabled: {self._lineout_device_label} source={content} volume={volume_i:.2f}"
        )

    def lineout_stop(self):
        """Stop the isolated audio pipeline without touching NDI."""
        self._lineout_pipeline.stop()
        self._lineout_pipeline._clear_status()
        with self._lock:
            was = self._lineout_enabled
            self._lineout_enabled = False
            self._lineout_device_id = None
            self._lineout_device_label = None
            self._lineout_sink_factory = None
            self._lineout_started_at = None
            self._lineout_last_error = None
        if was:
            self._lineout_log_push("Line output disabled")


    def start(self, input_url: str, ndi_name: str, channel_uuid: Optional[str] = None):
        """Start the pipeline.

        channel_uuid is optional but lets the rest of the system track the active channel.
        """
        self.start_with_delay(
            input_url=input_url,
            ndi_name=ndi_name,
            channel_uuid=channel_uuid,
        )

    def start_with_delay(
        self,
        input_url: str,
        ndi_name: str,
        channel_uuid: Optional[str] = None,
        delay_ms: Optional[int] = None,
        deinterlace: Optional[bool] = None,
        buffer_extra_ms: Optional[int] = None,
        ndi_qos: Optional[bool] = None,
        enable_bitrate_probe: Optional[bool] = None,
        ndi_groups: Optional[str] = None,
        ndi_multicast_enabled: Optional[bool] = None,
        ndi_multicast_netprefix: Optional[str] = None,
        ndi_multicast_netmask: Optional[str] = None,
        ndi_multicast_ttl: Optional[int] = None,
        source_mode: str = "tv",
    ):
        """Start the pipeline with a configurable output delay.

        The delay is implemented as buffering on both the audio and video branches.

        delay_ms is clamped to [ndi_delay_min_ms, ndi_delay_max_ms] from config.json.
        """
        self.stop()

        # Line output is disabled on every NDI pipeline rebuild; the supervisor
        # reopens it if the user left it enabled.
        with self._lock:
            self._lineout_enabled = False
            self._lineout_device_id = None
            self._lineout_device_label = None
            self._lineout_sink_factory = None
            self._lineout_volume = float(self._cfg.get("lineout_volume", 0.8))
            self._lineout_sink_sync = bool(self._cfg.get("lineout_sink_sync", True))
            self._lineout_started_at = None
            self._lineout_last_error = None
            self._lineout_log_full.clear()
            self._lineout_log_tail.clear()

        cfg = self._cfg
        source_mode_i = str(source_mode or "tv").strip().lower()
        if source_mode_i not in {"tv", "test_card"}:
            raise ValueError("Unsupported NDI source mode")

        # Resolve per-start overrides against config.json defaults.
        delay_min = int(cfg.get("ndi_delay_min_ms", 20))
        delay_max = int(cfg.get("ndi_delay_max_ms", 500))
        delay_default = int(cfg.get("ndi_delay_ms", 250))
        try:
            delay_ms_i = int(delay_default if delay_ms is None else delay_ms)
        except Exception:
            delay_ms_i = delay_default
        delay_ms_i = max(delay_min, min(delay_max, delay_ms_i))

        deinterlace_i = bool(cfg.get("ndi_deinterlace", False)) if deinterlace is None else bool(deinterlace)

        buffer_extra_default = int(cfg.get("ndi_buffer_extra_ms", 0))
        buffer_extra_max = int(cfg.get("ndi_buffer_extra_max_ms", 500))
        try:
            buffer_extra_ms_i = int(buffer_extra_default if buffer_extra_ms is None else buffer_extra_ms)
        except Exception:
            buffer_extra_ms_i = buffer_extra_default
        buffer_extra_ms_i = max(0, min(buffer_extra_max, buffer_extra_ms_i))

        ndi_qos_i = bool(cfg.get("ndi_qos", False)) if ndi_qos is None else bool(ndi_qos)

        enable_probe_i = bool(cfg.get("enable_bitrate_probe", False)) if enable_bitrate_probe is None else bool(enable_bitrate_probe)

        runtime_settings = write_ndi_runtime_config(
            cfg,
            ndi_groups=ndi_groups,
            ndi_multicast_enabled=ndi_multicast_enabled,
            ndi_multicast_netprefix=ndi_multicast_netprefix,
            ndi_multicast_netmask=ndi_multicast_netmask,
            ndi_multicast_ttl=ndi_multicast_ttl,
        )
        ndi_groups_i = runtime_settings["ndi_groups"]
        discovery_servers_i = runtime_settings["ndi_discovery_server"]
        multicast_enabled_i = runtime_settings["ndi_multicast_enabled"]
        multicast_netprefix_i = runtime_settings["ndi_multicast_netprefix"]
        multicast_netmask_i = runtime_settings["ndi_multicast_netmask"]
        multicast_ttl_i = runtime_settings["ndi_multicast_ttl"]
        multicast_label = (
            f"{multicast_netprefix_i}/{multicast_netmask_i} ttl={multicast_ttl_i}"
            if multicast_enabled_i
            else "off"
        )
        self._push_log(
            f"NDI runtime config: groups={ndi_groups_i or 'default'}; "
            f"discovery={discovery_servers_i or 'off'}; multicast={multicast_label}"
        )


        # Persist UI-facing metadata for /api/status.
        # These are cleared on stop(); when running, status_lite exposes them for the web UI.
        with self._lock:
            self._ndi_name = str(ndi_name)
            self._channel_uuid = channel_uuid
            self._input_url = str(input_url)
            self._source_mode = source_mode_i
            self._started_at = time.time()
            self._ndi_delay_ms = int(delay_ms_i)
            self._ndi_groups = ndi_groups_i or None
            self._ndi_discovery_server = discovery_servers_i or None
            self._ndi_multicast_enabled = bool(multicast_enabled_i)
            self._ndi_multicast_netprefix = multicast_netprefix_i if multicast_enabled_i else None
            self._ndi_multicast_netmask = multicast_netmask_i if multicast_enabled_i else None
            self._ndi_multicast_ttl = int(multicast_ttl_i) if multicast_enabled_i else None
            self._ndi_rendered = 0
            self._ndi_dropped = 0
            self._ndi_average_rate = 0.0
            self._ndi_stats_available = False
            self._ndi_last_stats_at = None
            self._dropped = 0
            self._ndi_fps_est = None
            self._fps_last_rendered = None
            self._fps_last_t = None


        with self._lock:
            self._bitrate_probe_enabled = enable_probe_i
            self._bitrate_probe_hooked = False
            self._bitrate_probe_bytes = 0
            self._bitrate_probe_last_t = None
            self._stats_cache_valid = False
            self._stats_ident = None
            self._stats_combiner = None
            self._stats_vpad = None
            self._stats_apad = None
            self._stats_ndisink = None
            self._stats_caps_last_t = 0.0

        # Note: min-threshold-time is in nanoseconds.
        delay_ns = int(delay_ms_i) * 1_000_000

        # Buffer caps are unknown at build time; these queues must be able to hold ~delay_ms worth of raw A/V.
        # We bound buffering in time (max-size-time) so the queue doesn't grow without limit.
        # Do not make max-size-time equal to min-threshold-time; live HTTP/DVB sources need headroom
        # or the queue can sit full/blocked and the NDI combiner never receives stable buffers.
        min_queue_time_ms = int(cfg.get("ndi_min_queue_time_ms", 500))
        queue_headroom_ms = int(cfg.get("ndi_queue_headroom_ms", 1000))
        max_time_ns = max(
            int(min_queue_time_ms) * 1_000_000,
            int(delay_ms_i + buffer_extra_ms_i) * 1_000_000,
            int(delay_ms_i + queue_headroom_ms) * 1_000_000,
        )

        # NDI audio output format.
        rate_hz = int(cfg.get("audio_rate_hz", 48000))
        channels = int(cfg.get("audio_channels", 2))

        ndi_video_format = str(cfg.get("ndi_video_format", "UYVY"))

        # Video processing for NDI. For interlaced sources (e.g., 1080i/50), software deinterlacing
        # is often the dominant CPU cost and can cause single-core spikes leading to stutter.
        # Default is deinterlace=False (send interlaced frames if the decoder provides them).
        def _video_processing_chain(src_prefix: str) -> str:
            if deinterlace_i:
                return (
                    f'{src_prefix} ! queue ! videoconvert ! deinterlace ! videoconvert '
                    f'! video/x-raw,format={ndi_video_format},interlace-mode=progressive '
                    f'! queue max-size-buffers=0 max-size-bytes=0 max-size-time={max_time_ns} '
                    f'min-threshold-time={delay_ns} ! combiner.video '
                )
            # Some MPEG decoders expose PAL/HD interlaced sources as "mixed", which
            # this ndisink build rejects. Preserve interlaced frames but normalise
            # the raw caps to a concrete interleaved mode for NDI negotiation.
            return (
                f'{src_prefix} ! queue ! videoconvert '
                f'! video/x-raw,format={ndi_video_format} '
                f'! capssetter caps=video/x-raw,format={ndi_video_format},interlace-mode=interleaved replace=false '
                f'! queue max-size-buffers=0 max-size-bytes=0 max-size-time={max_time_ns} '
                f'min-threshold-time={delay_ns} ! combiner.video '
            )

        def _audio_processing_chain(src_prefix: str) -> str:
            return (
                # Normalise audio timestamps before the delayed NDI branch.
                # audiorate helps prevent timestamp jitter/discontinuities from becoming audible artifacts
                # in downstream RTP receivers.
                f'{src_prefix} ! queue ! audioconvert ! audioresample ! audiorate '
                f'! audio/x-raw,rate={rate_hz},channels={channels},layout=interleaved '
                f'! queue max-size-buffers=0 max-size-bytes=0 max-size-time={max_time_ns} '
                f'min-threshold-time={delay_ns} ! combiner.audio '
            )

        probe_clause = (
            '! identity name=bitrateprobe silent=true signal-handoffs=true ' if enable_probe_i else '! '
        )

        # Tvheadend live streams are MPEG-TS over HTTP. The older default was a fixed
        # HEVC/AAC-LATM DVB-T2 path; that works for some 1080p50 services but fails
        # as soon as a scanned channel is H.264, MPEG-2, AC3, MP2, AAC-ADTS, etc.
        # Use uridecodebin3/uridecodebin as the automatic mixed-codec path because it
        # lets GStreamer choose the right demuxer/parser/decoder per service.
        # The explicit live_ts_* modes remain available via config.json for sites
        # that need to force a known broadcast codec pair.
        pipeline_mode = (
            "test_card"
            if source_mode_i == "test_card"
            else str(cfg.get("tvh_pipeline_mode", "uridecodebin3")).lower().strip()
        )
        auto_decode_modes = {"auto", "mixed", "mixed_codec", "uridecodebin3", "uridecodebin"}
        use_live_ts = pipeline_mode not in auto_decode_modes and str(input_url).lower().startswith(("http://", "https://"))

        def _live_ts_src() -> str:
            return f'souphttpsrc location={_gst_quote(input_url)} is-live=true do-timestamp=true ! tsdemux name=demux '

        def _make_live_ts_pipeline(video_src: str, audio_src: str) -> str:
            video_chain = _video_processing_chain(video_src)
            audio_chain = _audio_processing_chain(audio_src)
            return (
                f'{_live_ts_src()}'
                f'{video_chain}'
                f'{audio_chain}'
                f'ndisinkcombiner name=combiner {probe_clause}ndisink name=ndisink0 qos={"true" if ndi_qos_i else "false"} ndi-name={_gst_quote(ndi_name)}'
            )

        if source_mode_i == "test_card":
            width = max(640, min(1920, int(cfg.get("ndi_test_card_width", 1920))))
            height = max(360, min(1080, int(cfg.get("ndi_test_card_height", 1080))))
            fps = max(1, min(60, int(cfg.get("ndi_test_card_fps", 60))))
            tone_hz = max(100, min(4000, int(cfg.get("ndi_test_card_tone_hz", 1000))))
            tone_interval_ms = max(250, min(5000, int(cfg.get("ndi_test_card_tone_interval_ms", 1000))))
            tone_duration_ms = max(10, min(tone_interval_ms // 2, int(cfg.get("ndi_test_card_tone_duration_ms", 100))))
            tone_volume = max(0.01, min(1.0, float(cfg.get("ndi_test_card_tone_volume", 0.35))))
            sine_periods = max(1, int(round(tone_hz * tone_duration_ms / 1000.0)))
            motion_x, motion_y, motion_size = _test_card_motion_geometry(width, height)
            motion_center_x = motion_x + (motion_size // 2)
            motion_center_y = motion_y + (motion_size // 2)
            motion_radius = max(70, int(round(motion_size * 0.39)))
            marker_size = max(24, int(round(motion_size * 0.085)))
            background_path = _write_test_card_background(
                width=width,
                height=height,
                hostname=socket.gethostname(),
                ip_address=_primary_ipv4_address(),
                fps=fps,
                tone_hz=tone_hz,
                tone_interval_ms=tone_interval_ms,
                tone_duration_ms=tone_duration_ms,
                multicast_enabled=bool(multicast_enabled_i),
            )
            marker_path = _write_test_card_marker()
            with self._lock:
                self._test_card_background_path = background_path
                self._test_card_marker_path = marker_path
            pipeline_desc = (
                f'compositor name=testcardcompositor background=black max-threads=2 '
                f'sink_0::zorder=0 sink_1::xpos={motion_x} sink_1::ypos={motion_y} '
                f'sink_1::width={marker_size} sink_1::height={marker_size} sink_1::zorder=1 '
                f'! video/x-raw,format=BGRA,width={width},height={height},framerate={fps}/1,interlace-mode=progressive '
                f'! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! combiner.video '
                f'filesrc location={_gst_quote(background_path)} ! rsvgdec ! imagefreeze is-live=true '
                f'! video/x-raw,format=BGRA,width={width},height={height},framerate={fps}/1 '
                f'! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! testcardcompositor.sink_0 '
                f'filesrc location={_gst_quote(marker_path)} ! rsvgdec ! imagefreeze is-live=true '
                f'! video/x-raw,format=BGRA,width={marker_size},height={marker_size},framerate={fps}/1 '
                f'! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! testcardcompositor.sink_1 '
                f'audiotestsrc is-live=true do-timestamp=true wave=ticks freq={tone_hz} '
                f'volume={tone_volume:.3f} tick-interval={tone_interval_ms * 1_000_000} '
                f'sine-periods-per-tick={sine_periods} apply-tick-ramp=true samplesperbuffer=480 '
                f'! audio/x-raw,format=F32LE,rate={rate_hz},channels={channels},layout=interleaved '
                f'! tee name=testcardtone '
                f'testcardtone. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! combiner.audio '
                f'testcardtone. ! queue max-size-buffers=16 max-size-bytes=0 max-size-time=0 '
                f'! interaudiosink channel=teletool-test-card sync=true async=false '
                f'ndisinkcombiner name=combiner {probe_clause}'
                f'ndisink name=ndisink0 qos={"true" if ndi_qos_i else "false"} ndi-name={_gst_quote(ndi_name)}'
            )
        elif use_live_ts:
            # Explicit modes avoid parse-launch's ambiguous demux. ! decodebin linking when
            # services carry multiple audio tracks, teletext, subtitles, or mixed metadata.
            if pipeline_mode in {"live_ts_hevc_aac", "live_ts_h265_aac", "live_ts_dvbt2", "live_ts_explicit"}:
                pipeline_desc = _make_live_ts_pipeline(
                    'demux. ! queue ! h265parse ! avdec_h265',
                    'demux. ! queue ! aacparse ! avdec_aac_latm',
                )
            elif pipeline_mode in {"live_ts_h264_aac", "live_ts_avc_aac"}:
                pipeline_desc = _make_live_ts_pipeline(
                    'demux. ! queue ! h264parse ! decodebin caps=video/x-raw',
                    'demux. ! queue ! aacparse ! decodebin caps=audio/x-raw',
                )
            elif pipeline_mode in {"live_ts_mpeg2_mp2", "live_ts_mpeg2_mpa"}:
                pipeline_desc = _make_live_ts_pipeline(
                    'demux. ! queue ! mpegvideoparse ! decodebin caps=video/x-raw',
                    'demux. ! queue ! mpegaudioparse ! decodebin caps=audio/x-raw',
                )
            elif pipeline_mode in {"live_ts_hevc_ac3", "live_ts_h265_ac3"}:
                pipeline_desc = _make_live_ts_pipeline(
                    'demux. ! queue ! h265parse ! avdec_h265',
                    'demux. ! queue ! ac3parse ! decodebin caps=audio/x-raw',
                )
            elif pipeline_mode in {"live_ts_h264_ac3", "live_ts_avc_ac3"}:
                pipeline_desc = _make_live_ts_pipeline(
                    'demux. ! queue ! h264parse ! decodebin caps=video/x-raw',
                    'demux. ! queue ! ac3parse ! decodebin caps=audio/x-raw',
                )
            else:
                # Worldwide fallback: keep a generic path available, but do not use it by default
                # because it reproduced the black-screen/no-caps condition on DVB-T2 HEVC/AAC.
                pipeline_desc = _make_live_ts_pipeline(
                    'demux. ! queue ! decodebin caps=video/x-raw',
                    'demux. ! queue ! decodebin caps=audio/x-raw',
                )
        else:
            # Automatic mixed-codec path. uridecodebin3 is preferred when available
            # because it uses decodebin3's stream-selection logic for transport
            # streams with multiple audio/subtitle/metadata streams. Fall back to
            # uridecodebin on older GStreamer installs. Both expose raw audio/video
            # pads, so the downstream raw caps select the appropriate branch.
            video_chain = _video_processing_chain('d.')
            audio_chain = _audio_processing_chain('d.')
            decodebin_element = "uridecodebin3"
            if Gst.ElementFactory.find("uridecodebin3") is None:
                decodebin_element = "uridecodebin"
            decodebin_props = f'uri={_gst_quote(input_url)} name=d'
            if decodebin_element == "uridecodebin":
                decodebin_props += ' expose-all-streams=false'
            pipeline_desc = (
                f'{decodebin_element} {decodebin_props} '
                f'{video_chain}'
                f'{audio_chain}'
                f'ndisinkcombiner name=combiner {probe_clause}ndisink name=ndisink0 qos={"true" if ndi_qos_i else "false"} ndi-name={_gst_quote(ndi_name)}'
            )

        with self._lock:
            self._stats_cache_valid = False
            self._stats_ident = None
            self._stats_combiner = None
            self._stats_vpad = None
            self._stats_apad = None
            self._stats_ndisink = None
            self._stats_caps_last_t = 0.0

        self._push_log(f"NDI pipeline mode: {pipeline_mode}; delay={delay_ms_i}ms; deinterlace={deinterlace_i}")
        self._push_log(f"NDI pipeline: {pipeline_desc}")
        self._start_pipeline(pipeline_desc=pipeline_desc, poll_cb=self._poll_stats)
        if source_mode_i == "test_card":
            try:
                self._wait_until_playing(timeout_s=8.0)
                self._call_in_gst_context_sync(
                    lambda: self._setup_test_card_motion(
                        motion_center_x,
                        motion_center_y,
                        motion_radius,
                        marker_size,
                    )
                )
            except Exception:
                self.stop()
                raise


    def stop(self):
        # Stop line output first (if active)
        try:
            self.lineout_stop()
        except Exception:
            pass

        super().stop()
        with self._lock:
            asset_paths = (
                self._test_card_background_path,
                self._test_card_marker_path,
            )
            self._test_card_background_path = None
            self._test_card_marker_path = None
            self._test_card_controls = []
        for asset_path in asset_paths:
            if asset_path is not None:
                try:
                    asset_path.unlink(missing_ok=True)
                except Exception:
                    pass
        with self._lock:
            self._ndi_name = None
            self._channel_uuid = None
            self._input_url = None
            self._source_mode = None
            self._started_at = None
            self._ndi_delay_ms = None
            self._ndi_groups = None
            self._ndi_discovery_server = None
            self._ndi_multicast_enabled = False
            self._ndi_multicast_netprefix = None
            self._ndi_multicast_netmask = None
            self._ndi_multicast_ttl = None
            self._bitrate_bps_est = None
            self._bitrate_probe_hooked = False
            self._bitrate_probe_bytes = 0
            self._bitrate_probe_last_t = None

    # ---------- Base hooks ----------

    def _on_bus_message_extra(self, msg: Gst.Message) -> bool:
        if msg.type == Gst.MessageType.QOS:
            with self._lock:
                self._qos_events += 1
        return True

    # ---------- Monitoring ----------

    def _set_video_caps(self, s: str):
        with self._lock:
            self._video_caps = s

    def _set_audio_caps(self, s: str):
        with self._lock:
            self._audio_caps = s

    def _caps_summary(self, caps: Gst.Caps) -> str:
        try:
            st = caps.get_structure(0)
            if not st:
                return caps.to_string()
            return st.to_string()
        except Exception:
            return caps.to_string()

    def _on_bitrate_handoff(self, _identity, buf, _pad):
        """GStreamer handoff callback used to estimate bitrate into NDI (best-effort)."""
        try:
            n = int(buf.get_size())
        except Exception:
            return
        with self._lock:
            self._bitrate_probe_bytes += n

    def _refresh_stats_cache(self, pipeline: Gst.Pipeline):
        with self._lock:
            if self._stats_cache_valid:
                return
        ident = None
        combiner = None
        vpad = None
        apad = None
        ndisink = None
        try:
            ident = pipeline.get_by_name("bitrateprobe")
        except Exception:
            ident = None
        try:
            combiner = pipeline.get_by_name("combiner")
            if combiner is not None:
                vpad = combiner.get_static_pad("video")
                apad = combiner.get_static_pad("audio")
        except Exception:
            combiner = None
            vpad = None
            apad = None
        try:
            ndisink = pipeline.get_by_name("ndisink0")
        except Exception:
            ndisink = None
        with self._lock:
            self._stats_ident = ident
            self._stats_combiner = combiner
            self._stats_vpad = vpad
            self._stats_apad = apad
            self._stats_ndisink = ndisink
            self._stats_cache_valid = True

    def _poll_stats(self) -> bool:
        with self._lock:
            pipeline = self._pipeline
        if pipeline is None:
            return False

        self._refresh_stats_cache(pipeline)

        with self._lock:
            ident = self._stats_ident
            combiner = self._stats_combiner
            vpad = self._stats_vpad
            apad = self._stats_apad
            ndisink = self._stats_ndisink
            caps_last_t = self._stats_caps_last_t
            need_caps = (self._video_caps is None or self._audio_caps is None)

        # Optional NDI bitrate probe (identity element inserted between combiner and ndisink).
        if self._bitrate_probe_enabled and not self._bitrate_probe_hooked and ident is not None:
            try:
                ident.connect("handoff", self._on_bitrate_handoff)
                with self._lock:
                    self._bitrate_probe_hooked = True
                    self._bitrate_probe_bytes = 0
                    self._bitrate_probe_last_t = time.time()
            except Exception:
                pass

        # Caps from combiner sink pads: populate immediately, then refresh occasionally.
        if combiner and (need_caps or (time.time() - caps_last_t) >= 10.0):
            try:
                v_caps = vpad.get_current_caps() if vpad else None
                a_caps = apad.get_current_caps() if apad else None
                if v_caps:
                    self._set_video_caps(self._caps_summary(v_caps))
                if a_caps:
                    self._set_audio_caps(self._caps_summary(a_caps))
                with self._lock:
                    self._stats_caps_last_t = time.time()
            except Exception:
                pass

        # ndisink stats
        if ndisink:
            try:
                st = ndisink.get_property("stats")
                if st:
                    avg = float(st.get_value("average-rate")) if st.has_field("average-rate") else 0.0
                    drp = int(st.get_value("dropped")) if st.has_field("dropped") else 0
                    rnd = int(st.get_value("rendered")) if st.has_field("rendered") else 0

                    now = time.time()
                    with self._lock:
                        self._ndi_average_rate = avg
                        self._ndi_stats_available = True
                        self._ndi_last_stats_at = now
                        self._ndi_dropped = drp
                        self._ndi_rendered = rnd
                        self._dropped = drp

                        # fps estimate from rendered deltas
                        if self._fps_last_rendered is None or self._fps_last_t is None:
                            self._fps_last_rendered = rnd
                            self._fps_last_t = now
                            self._ndi_fps_est = None
                        else:
                            dt = now - self._fps_last_t
                            df = rnd - self._fps_last_rendered
                            if dt > 0 and df >= 0:
                                inst = df / dt
                                if self._ndi_fps_est is None:
                                    self._ndi_fps_est = inst
                                else:
                                    self._ndi_fps_est = (0.7 * self._ndi_fps_est) + (0.3 * inst)

                            self._fps_last_rendered = rnd
                            self._fps_last_t = now
            except Exception as e:
                with self._lock:
                    self._ndi_stats_available = False
                    self._last_warning = f"NDI stats unavailable: {e}"

        # Update bitrate estimate once per poll tick.
        if self._bitrate_probe_enabled and self._bitrate_probe_hooked:
            now = time.time()
            with self._lock:
                last = self._bitrate_probe_last_t
                b = self._bitrate_probe_bytes
                if last is None:
                    self._bitrate_probe_last_t = now
                    self._bitrate_probe_bytes = 0
                else:
                    dt = now - last
                    if dt > 0:
                        self._bitrate_bps_est = int((b * 8) / dt)
                    self._bitrate_probe_last_t = now
                    self._bitrate_probe_bytes = 0

        return True
