import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, List, Optional
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

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


from gst_base import GstPipelineBase

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # type: ignore


@dataclass
class RunState:
    running: bool
    pid: Optional[int]
    channel_uuid: Optional[str]
    ndi_name: Optional[str]
    input_url: Optional[str]
    started_at: Optional[float]
    last_log: List[str]

    pipeline_state: str
    video_caps: Optional[str]
    audio_caps: Optional[str]

    # configured NDI delay (applied as buffering in the pipeline)
    ndi_delay_ms: Optional[int]
    ndi_groups: Optional[str]
    ndi_discovery_server: Optional[str]

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

        # Config-driven feature toggles / defaults (may be overridden per-start).
        self._bitrate_probe_enabled: bool = bool(self._cfg.get("enable_bitrate_probe", False))
        self._bitrate_probe_hooked: bool = False
        self._bitrate_probe_bytes: int = 0
        self._bitrate_probe_last_t: Optional[float] = None

        self._ndi_name: Optional[str] = None
        self._channel_uuid: Optional[str] = None
        self._input_url: Optional[str] = None
        self._started_at: Optional[float] = None

        self._video_caps: Optional[str] = None
        self._audio_caps: Optional[str] = None

        # Configured NDI delay (ms) for the currently running pipeline
        self._ndi_delay_ms: Optional[int] = None
        self._ndi_groups: Optional[str] = None
        self._ndi_discovery_server: Optional[str] = None

        # NDI multicast (per-stream overrides; only meaningful while running)
        self._ndi_multicast_enabled: bool = False
        self._ndi_multicast_addr: Optional[str] = None
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

    @staticmethod
    def _has_property(element: Any, name: str) -> bool:
        try:
            return element.find_property(name) is not None
        except Exception:
            return False

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

    def audio_output_devices(self) -> List[Dict[str, Any]]:
        """Detect supported local outputs and the optional Inferno ALSA PCM."""
        sink_factory = self._select_lineout_sink_factory()
        devices: List[Dict[str, Any]] = []
        seen = set()
        inferno_device = str(self._cfg.get("inferno_alsa_device", "teletool_inferno") or "").strip()

        def add(device_id: str, label: str, *, device: Optional[str], sink: str, kind: str, details: str = "") -> None:
            if device_id in seen:
                return
            seen.add(device_id)
            devices.append(
                {
                    "id": device_id,
                    "label": label,
                    "sink": sink,
                    "device": device,
                    "kind": kind,
                    "details": details,
                    "experimental": kind == "inferno",
                    "network_output": kind == "inferno",
                    "sample_format": "S32LE" if kind == "inferno" else None,
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
                return "Dante-compatible network output (Inferno, experimental)"
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
                            detail = f"Experimental Inferno ALSA PCM ({name})"
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
                started_at=self._started_at if base["running"] else None,
                last_log=base["last_log"],
                pipeline_state=base["pipeline_state"],
                video_caps=self._video_caps,
                audio_caps=self._audio_caps,
                ndi_delay_ms=self._ndi_delay_ms,
                ndi_groups=self._ndi_groups if base["running"] else None,
                ndi_discovery_server=self._ndi_discovery_server if base["running"] else None,
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
                "started_at": self._started_at if running else None,
                "ndi_groups": self._ndi_groups if running else None,
                "ndi_discovery_server": self._ndi_discovery_server if running else None,
                "ndi_multicast_enabled": bool(self._ndi_multicast_enabled) if running else False,
                "ndi_multicast_addr": self._ndi_multicast_addr if running else None,
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
        base = self._base_status_fields(include_log=False)
        with self._lock:
            enabled = bool(self._lineout_enabled)
            running = bool(base["running"]) and enabled
            st = LineOutRunState(
                running=running,
                device_id=self._lineout_device_id if enabled else None,
                device_label=self._lineout_device_label if enabled else None,
                sink=self._lineout_sink_factory if enabled else None,
                channel_uuid=self._channel_uuid if base["running"] else None,
                input_url=self._input_url if base["running"] else None,
                started_at=self._lineout_started_at if enabled else None,
                last_log=list(self._lineout_log_tail) if include_logs else [],
                pipeline_state=base["pipeline_state"],
                last_error=self._lineout_last_error,
                volume=float(self._lineout_volume) if enabled else None,
                sink_sync=bool(self._lineout_sink_sync) if enabled else None,
            )
            d = asdict(st)
            d["ndi_running"] = bool(base["running"])
            return d

    def lineout_start(self, device_id: Optional[str] = None, volume: Optional[float] = None):
        """Enable/configure the local line-level audio branch inside the running pipeline."""
        base = self._base_status_fields(include_log=False)
        if not base.get("running"):
            raise RuntimeError("NDI pipeline must be running before line output can be started")

        selected = self._resolve_audio_output_device(device_id or self._cfg.get("lineout_default_device"))
        sink_factory = str(selected.get("sink") or "")
        selected_kind = str(selected.get("kind") or "")
        if sink_factory == "fakesink":
            raise RuntimeError(selected.get("details") or "No usable audio output sink found")

        try:
            volume_i = float(self._cfg.get("lineout_volume", 0.8) if volume is None else volume)
        except Exception:
            volume_i = 0.8
        volume_i = max(0.0, min(1.0, volume_i))
        sink_sync = bool(self._cfg.get("lineout_sink_sync", True))

        def _apply():
            with self._lock:
                pipeline = self._pipeline
            if pipeline is None:
                raise RuntimeError("Pipeline not running")

            sink = pipeline.get_by_name("lineoutsink")
            valve = pipeline.get_by_name("lineoutvalve")
            volume_el = pipeline.get_by_name("lineoutvolume")
            if sink is None or valve is None or volume_el is None:
                raise RuntimeError("Line output elements not found in pipeline")
            actual_factory = sink.get_factory().get_name() if sink.get_factory() is not None else ""
            if sink_factory != actual_factory:
                raise RuntimeError(f"Selected device requires {sink_factory}, but pipeline was built with {actual_factory}")

            valve.set_property("drop", True)
            device_changed = False
            if selected.get("device") and self._has_property(sink, "device"):
                selected_device = str(selected["device"])
                current_device = str(sink.get_property("device") or "")
                if current_device != selected_device:
                    # ALSA opens its PCM during state changes. Recycle only the
                    # sink so a virtual PCM selected after the NDI pipeline was
                    # built is actually opened, without restarting NDI.
                    sink.set_state(Gst.State.NULL)
                    sink.set_property("device", selected_device)
                    device_changed = True
            if self._has_property(sink, "sync"):
                sink.set_property("sync", sink_sync)
            if self._has_property(sink, "async"):
                sink.set_property("async", False)
            if selected_kind == "inferno":
                if self._has_property(sink, "provide-clock"):
                    sink.set_property("provide-clock", False)
                if self._has_property(sink, "slave-method"):
                    sink.set_property("slave-method", 0)  # resample to the NDI/pipeline clock
                if self._has_property(sink, "buffer-time"):
                    sink.set_property("buffer-time", max(21333, int(self._cfg.get("inferno_alsa_buffer_time_us", 85333))))
                if self._has_property(sink, "latency-time"):
                    sink.set_property("latency-time", max(1333, int(self._cfg.get("inferno_alsa_latency_time_us", 5333))))
            if device_changed and not sink.sync_state_with_parent():
                raise RuntimeError(f"Could not open selected ALSA output: {selected_device}")
            volume_el.set_property("volume", volume_i)
            valve.set_property("drop", False)

        try:
            self._call_in_gst_context_sync(_apply, timeout_s=8.0 if selected_kind == "inferno" else 2.0)
        except Exception as e:
            with self._lock:
                self._lineout_last_error = str(e)
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
        self._lineout_log_push(f"Line output enabled: {self._lineout_device_label} volume={volume_i:.2f}")

    def lineout_stop(self):
        """Disable local line output by closing the branch valve."""
        def _apply():
            with self._lock:
                pipeline = self._pipeline
            if pipeline is None:
                return
            valve = pipeline.get_by_name("lineoutvalve")
            if valve is not None:
                valve.set_property("drop", True)

        self._call_in_gst_context(_apply)

        with self._lock:
            was = self._lineout_enabled
            self._lineout_enabled = False
            self._lineout_device_id = None
            self._lineout_device_label = None
            self._lineout_sink_factory = None
            self._lineout_started_at = None
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

    @staticmethod
    def _normalise_ndi_groups(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        groups: List[str] = []
        seen = set()
        for raw in re.split(r"[,;\n]+", text):
            group = " ".join(str(raw or "").strip().split())
            if not group:
                continue
            if any(ord(ch) < 32 for ch in group):
                raise ValueError("NDI group names cannot contain control characters")
            if len(group) > 64:
                raise ValueError("Each NDI group name must be 64 characters or fewer")
            key = group.lower()
            if key not in seen:
                groups.append(group)
                seen.add(key)
            if len(groups) > 16:
                raise ValueError("Use no more than 16 NDI groups")
        result = ",".join(groups)
        if len(result) > 240:
            raise ValueError("NDI group list is too long")
        return result

    @staticmethod
    def _normalise_ndi_discovery_servers(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        servers: List[str] = []
        seen = set()
        for raw in re.split(r"[,\s]+", text):
            token = str(raw or "").strip()
            if not token:
                continue
            host = token
            port: Optional[int] = None
            if token.count(":") == 1:
                host_part, port_part = token.rsplit(":", 1)
                if port_part:
                    try:
                        port = int(port_part)
                    except ValueError:
                        raise ValueError("NDI Discovery Server ports must be numeric")
                    if port < 1 or port > 65535:
                        raise ValueError("NDI Discovery Server ports must be between 1 and 65535")
                    host = host_part
            host = host.strip().strip("[]")
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                raise ValueError("Enter NDI Discovery Server IP addresses only")
            normalised = str(ip)
            if port is not None:
                normalised = f"{normalised}:{port}"
            key = normalised.lower()
            if key not in seen:
                servers.append(normalised)
                seen.add(key)
            if len(servers) > 8:
                raise ValueError("Use no more than 8 NDI Discovery Server addresses")
        return ",".join(servers)

    @staticmethod
    def _ndi_runtime_config_path() -> Path:
        configured = str(os.environ.get("TELETOOL_NDI_CONFIG_PATH") or "").strip()
        if configured:
            return Path(configured).expanduser()
        home = str(os.environ.get("HOME") or "").strip()
        if home:
            return Path(home).expanduser() / ".ndi" / "ndi-config.v1.json"
        return Path.home() / ".ndi" / "ndi-config.v1.json"

    def _write_ndi_runtime_config(self, ndi_groups: str, discovery_servers: str) -> None:
        path = self._ndi_runtime_config_path()
        if not ndi_groups and not discovery_servers and not path.exists():
            return

        root: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    root = loaded
                else:
                    self._push_warn(f"Ignoring invalid NDI config root at {path}")
            except Exception as e:
                self._push_warn(f"Replacing unreadable NDI config at {path}: {e}")

        ndi = root.get("ndi")
        if not isinstance(ndi, dict):
            ndi = {}
            root["ndi"] = ndi

        networks = ndi.get("networks")
        if not isinstance(networks, dict):
            networks = {}
        if discovery_servers:
            networks["discovery"] = discovery_servers
        else:
            networks.pop("discovery", None)
        if networks:
            ndi["networks"] = networks
        else:
            ndi.pop("networks", None)

        groups = ndi.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        if ndi_groups:
            groups["send"] = ndi_groups
            groups["recv"] = ndi_groups
        else:
            groups.pop("send", None)
            groups.pop("recv", None)
        if groups:
            ndi["groups"] = groups
        else:
            ndi.pop("groups", None)

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(json.dumps(root, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

        group_label = ndi_groups or "default"
        discovery_label = discovery_servers or "off"
        self._push_log(f"NDI runtime config: groups={group_label}; discovery={discovery_label}")

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
        ndi_multicast_addr: Optional[str] = None,
        ndi_multicast_ttl: Optional[int] = None,
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

        ndi_groups_i = self._normalise_ndi_groups(cfg.get("ndi_groups", "") if ndi_groups is None else ndi_groups)
        discovery_servers_i = self._normalise_ndi_discovery_servers(cfg.get("ndi_discovery_server", ""))
        self._write_ndi_runtime_config(ndi_groups_i, discovery_servers_i)

        # NDI multicast per-stream overrides (best-effort; depends on ndisink implementation)
        multicast_enabled_default = bool(cfg.get("ndi_multicast_enabled", False))
        multicast_enabled_i = multicast_enabled_default if ndi_multicast_enabled is None else bool(ndi_multicast_enabled)

        multicast_ttl_default = int(cfg.get("ndi_multicast_ttl", 1))
        try:
            multicast_ttl_i = int(multicast_ttl_default if ndi_multicast_ttl is None else ndi_multicast_ttl)
        except Exception:
            multicast_ttl_i = multicast_ttl_default
        multicast_ttl_i = max(0, min(255, multicast_ttl_i))

        multicast_addr_default = str(cfg.get("ndi_multicast_addr", ""))  # optional
        multicast_addr_i = multicast_addr_default if ndi_multicast_addr is None else str(ndi_multicast_addr or "")

        if multicast_enabled_i and not multicast_addr_i.strip():
            raise ValueError("NDI multicast is enabled but no multicast address was provided")


        # Persist UI-facing metadata for /api/status.
        # These are cleared on stop(); when running, status_lite exposes them for the web UI.
        with self._lock:
            self._ndi_name = str(ndi_name)
            self._channel_uuid = channel_uuid
            self._input_url = str(input_url)
            self._started_at = time.time()
            self._ndi_delay_ms = int(delay_ms_i)
            self._ndi_groups = ndi_groups_i or None
            self._ndi_discovery_server = discovery_servers_i or None
            self._ndi_multicast_enabled = bool(multicast_enabled_i)
            self._ndi_multicast_addr = multicast_addr_i.strip() if multicast_enabled_i else None
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

        # Shared audio format used for NDI and line-output branches
        rate_hz = int(cfg.get("audio_rate_hz", 48000))
        channels = int(cfg.get("audio_channels", 2))

        # Line output branch defaults (inactive until /api/audio/start opens the valve).
        lineout_queue_time_ns = int(cfg.get("lineout_queue_time_ms", 200)) * 1_000_000
        lineout_sink_factory = self._select_lineout_sink_factory()
        lineout_sink_sync = bool(cfg.get("lineout_sink_sync", True))
        try:
            lineout_pipeline_volume = float(cfg.get("lineout_volume", 0.8))
        except Exception:
            lineout_pipeline_volume = 0.8
        lineout_pipeline_volume = max(0.0, min(1.0, lineout_pipeline_volume))
        lineout_default_device = ""
        lineout_default_kind = ""
        if lineout_sink_factory == "alsasink":
            try:
                default_lineout = self._resolve_audio_output_device(cfg.get("lineout_default_device"))
                if default_lineout.get("sink") == "alsasink" and default_lineout.get("device"):
                    lineout_default_device = str(default_lineout["device"])
                    lineout_default_kind = str(default_lineout.get("kind") or "")
                else:
                    lineout_sink_factory = "fakesink"
            except Exception:
                lineout_sink_factory = "fakesink"

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
            if lineout_sink_factory == "alsasink":
                inferno_sink_props = ""
                if lineout_default_kind == "inferno":
                    inferno_buffer_time_us = max(21333, int(cfg.get("inferno_alsa_buffer_time_us", 85333)))
                    inferno_latency_time_us = max(1333, int(cfg.get("inferno_alsa_latency_time_us", 5333)))
                    inferno_sink_props = (
                        f'provide-clock=false slave-method=resample '
                        f'buffer-time={inferno_buffer_time_us} latency-time={inferno_latency_time_us} '
                    )
                lineout_sink = (
                    f'alsasink name=lineoutsink device={_gst_quote(lineout_default_device)} '
                    f'{inferno_sink_props}async=false sync={"true" if lineout_sink_sync else "false"} '
                )
            elif lineout_sink_factory == "autoaudiosink":
                lineout_sink = (
                    f'autoaudiosink name=lineoutsink '
                    f'async=false sync={"true" if lineout_sink_sync else "false"} '
                )
            elif lineout_sink_factory == "pulsesink":
                lineout_sink = (
                    f'pulsesink name=lineoutsink '
                    f'async=false sync={"true" if lineout_sink_sync else "false"} '
                )
            else:
                lineout_sink = 'fakesink name=lineoutsink async=false sync=false '

            lineout_caps = (
                f'audio/x-raw,format=S32LE,rate={rate_hz},channels={channels},layout=interleaved '
                if lineout_default_kind == "inferno"
                else f'audio/x-raw,rate={rate_hz},channels={channels},layout=interleaved '
            )

            return (
                # audio decode/convert → audiorate (perfect timestamps) → tee
                # audiorate helps prevent timestamp jitter/discontinuities from becoming audible artifacts
                # in downstream RTP receivers.
                f'{src_prefix} ! queue ! audioconvert ! audioresample ! audiorate '
                f'! audio/x-raw,rate={rate_hz},channels={channels},layout=interleaved ! tee name=atee '
                # audio → delayed → NDI
                f'atee. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time={max_time_ns} '
                f'min-threshold-time={delay_ns} ! combiner.audio '
                # audio -> line output (no NDI delay; valve closed by default)
                f'atee. ! valve name=lineoutvalve drop=true '
                f'! queue leaky=downstream max-size-buffers=0 max-size-bytes=0 max-size-time={lineout_queue_time_ns} '
                f'! audioconvert ! audioresample '
                f'! {lineout_caps}'
                f'! volume name=lineoutvolume volume={lineout_pipeline_volume:.3f} '
                f'! {lineout_sink}'
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
        pipeline_mode = str(cfg.get("tvh_pipeline_mode", "uridecodebin3")).lower().strip()
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

        if use_live_ts:
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

        # Apply multicast settings after pipeline creation (thread-safe).
        # We intentionally do this as a best-effort operation so that older/other ndisink builds
        # without multicast support still work.
        def _apply_mcast():
            with self._lock:
                pipeline = self._pipeline
            if pipeline is None:
                return
            sink = pipeline.get_by_name("ndisink0")
            if sink is None:
                return
            try_props = []
            if multicast_enabled_i:
                # Common property name candidates across NDI sinks.
                try_props = [
                    ("multicast", True),
                    ("multicast-enabled", True),
                    ("enable-multicast", True),
                ]
                for prop, val in try_props:
                    try:
                        sink.set_property(prop, val)
                        break
                    except Exception:
                        pass
                # Address
                for prop in ("multicast-address", "multicast-addr", "multicast_addr"):
                    try:
                        sink.set_property(prop, multicast_addr_i.strip())
                        break
                    except Exception:
                        pass
                # TTL
                for prop in ("multicast-ttl", "multicast_ttl", "ttl-mc", "ttl_mc"):
                    try:
                        sink.set_property(prop, int(multicast_ttl_i))
                        break
                    except Exception:
                        pass
            else:
                for prop in ("multicast", "multicast-enabled", "enable-multicast"):
                    try:
                        sink.set_property(prop, False)
                        break
                    except Exception:
                        pass

        self._call_in_gst_context(_apply_mcast)


    def stop(self):
        # Stop line output first (if active)
        try:
            self.lineout_stop()
        except Exception:
            pass

        super().stop()
        with self._lock:
            self._ndi_name = None
            self._channel_uuid = None
            self._input_url = None
            self._started_at = None
            self._ndi_delay_ms = None
            self._ndi_groups = None
            self._ndi_discovery_server = None
            self._ndi_multicast_enabled = False
            self._ndi_multicast_addr = None
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
