import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import threading
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import requests
import fleet_manager
import system_manager
from tvh import TELETOOL_UK_AUTO_SCANFILE, TvheadendClient
from gst_ndi import GstNDIBridge
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("TELETOOL_CONFIG_PATH", str(BASE_DIR / "config.json"))).expanduser()
CONFIG_LOCK = threading.Lock()
NDI_RUNTIME_NAME = "libndi.so.6"
NDI_SDK_URL = "https://ndi.video/for-developers/ndi-sdk/"
NDI_RUNTIME_PATH = Path(os.environ.get("TELETOOL_NDI_RUNTIME_PATH", "/usr/local/lib/libndi.so.6")).expanduser()
NDI_DROP_PATH = Path(os.environ.get("TELETOOL_NDI_LIB", str(Path.home() / NDI_RUNTIME_NAME))).expanduser()
NDI_INSTALL_HELPER = Path(
    os.environ.get("TELETOOL_NDI_INSTALL_HELPER", "/usr/lib/teletool/bin/install-ndi-runtime")
).expanduser()
NDI_VERIFICATION_MARKER = Path(
    os.environ.get("TELETOOL_NDI_VERIFICATION_MARKER", "/var/lib/teletool/ndi-runtime-verified")
).expanduser()
NDI_UPLOAD_MIN_BYTES = 64 * 1024
NDI_UPLOAD_MAX_BYTES = 128 * 1024 * 1024
NDI_UPLOAD_LOCK = threading.Lock()


def _ndi_runtime_status() -> Dict[str, Any]:
    runtime_dir = str(os.environ.get("NDI_RUNTIME_DIR_V6") or "").strip()
    runtime_candidates = [NDI_RUNTIME_PATH]
    if runtime_dir:
        runtime_candidates.append(Path(runtime_dir).expanduser() / NDI_RUNTIME_NAME)

    installed_path = next((path for path in runtime_candidates if path.is_file()), None)
    installed = installed_path is not None
    verified = NDI_VERIFICATION_MARKER.is_file()
    staged = NDI_DROP_PATH.is_file()
    return {
        "ready": installed and verified,
        "installed": installed,
        "verified": verified,
        "installed_path": str(installed_path) if installed_path else None,
        "staged": staged,
        "drop_path": str(NDI_DROP_PATH),
        "drop_directory": str(NDI_DROP_PATH.parent),
        "runtime_name": NDI_RUNTIME_NAME,
        "sdk_url": NDI_SDK_URL,
        "setup_command": "wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh",
        "upload_enabled": NDI_INSTALL_HELPER.is_file() and os.access(NDI_INSTALL_HELPER, os.X_OK),
        "upload_max_bytes": NDI_UPLOAD_MAX_BYTES,
    }


def _validate_ndi_upload_header(path: Path, size: int) -> None:
    if size < NDI_UPLOAD_MIN_BYTES:
        raise HTTPException(400, "The uploaded file is too small to be the NDI runtime library.")
    with path.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise HTTPException(400, "The uploaded file is not an ELF shared library.")
    if header[4] != 2:
        raise HTTPException(400, "The uploaded file is not a 64-bit ELF library.")
    if header[5] not in {1, 2}:
        raise HTTPException(400, "The uploaded file uses an unsupported ELF byte order.")
    byte_order = "little" if header[5] == 1 else "big"
    machine = int.from_bytes(header[18:20], byte_order)
    if machine != 183:
        raise HTTPException(400, "The uploaded file is not built for ARM64/AArch64.")


def _load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError("Missing config.json (create it and set tvh_base_url).")
    return json.loads(CONFIG_PATH.read_text())


def _save_config(next_cfg: Dict[str, Any]) -> Dict[str, Any]:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(next_cfg, indent=2) + "\n"
    existing_mode = None
    try:
        existing_mode = CONFIG_PATH.stat().st_mode & 0o777
    except OSError:
        pass

    fd, tmp_name = tempfile.mkstemp(prefix=f".{CONFIG_PATH.name}.", dir=str(CONFIG_PATH.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, existing_mode if existing_mode is not None else 0o640)
        os.replace(tmp_path, CONFIG_PATH)
        # Persist the directory entry as well as the file contents on Linux.
        try:
            dir_fd = os.open(CONFIG_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return next_cfg


def _build_tvh_client(config: Dict[str, Any]) -> TvheadendClient:
    tvh_auth = None
    if config.get("tvh_username") and config.get("tvh_password") is not None:
        tvh_auth = (str(config.get("tvh_username")), str(config.get("tvh_password")))
    return TvheadendClient(
        base_url=config.get("tvh_base_url", "http://127.0.0.1:9981"),
        timeout_s=float(config.get("tvh_read_timeout_s", 10)),
        connect_timeout_s=float(config.get("tvh_connect_timeout_s", 3)),
        retries=int(config.get("tvh_retries", 3)),
        backoff_s=float(config.get("tvh_backoff_s", 0.4)),
        verify_tls=bool(config.get("tvh_verify_tls", True)),
        auth=tvh_auth,
    )


def _update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    global cfg, _active_profile, NDI_DELAY_DEFAULT_MS, tvh, ndi_bridge
    with CONFIG_LOCK:
        next_cfg = deepcopy(cfg)
        next_cfg.update(patch)
        cfg = _save_config(next_cfg)
        _active_profile = str(cfg.get("tvh_stream_profile", "pass"))
        NDI_DELAY_DEFAULT_MS = int(cfg.get("ndi_delay_ms", 250))

        # Keep live runtime objects coherent with the saved config. Future stream
        # starts/line-output operations use the new bridge defaults immediately, and
        # tvheadend API calls use the new base URL/auth/retry settings without
        # requiring an application restart.
        try:
            old_tvh = tvh
        except NameError:
            old_tvh = None
        tvh = _build_tvh_client(cfg)
        if old_tvh is not None:
            try:
                old_tvh.close()
            except Exception:
                pass
        try:
            ndi_bridge.update_config(cfg)
        except NameError:
            pass

        return deepcopy(cfg)


def _update_stored_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    global cfg
    with CONFIG_LOCK:
        next_cfg = deepcopy(cfg)
        next_cfg.update(patch)
        cfg = _save_config(next_cfg)
        return deepcopy(cfg)


cfg: Dict[str, Any] = {}
tvh: Any = None
ndi_bridge: Any = None

# TVH stream profile used to resolve the current channel's stream URL.
_active_profile: str = "pass"

# Default (fixed) NDI delay applied when starting the pipeline
NDI_DELAY_DEFAULT_MS: int = 250


# ---------------- NDI supervision / auto-reconnect ----------------
# The live tvheadend stream is consumed by GStreamer, not by TvheadendClient.
# If tvheadend drops/stalls the HTTP stream, GStreamer can post ERROR/EOS or
# simply stop rendering frames. This supervisor owns the desired channel state
# and restarts the NDI pipeline with a freshly resolved tvheadend URL.
NDI_SUPERVISOR_LOCK = threading.RLock()
NDI_SUPERVISOR_STOP = threading.Event()
NDI_SUPERVISOR_THREAD: Optional[threading.Thread] = None
NDI_SUPERVISOR_STATE: Dict[str, Any] = {
    "desired": False,
    "request": None,
    "last_start_attempt_at": None,
    "last_success_at": None,
    "last_stop_at": None,
    "was_running": False,
    "restart_count": 0,
    "last_restart_reason": None,
    "last_stream_url": None,
    "last_error": None,
    "last_rendered": None,
    "last_rendered_change_at": None,
    "healthy_since": None,
    "pipeline_status": "stopped",
    "lineout_desired": False,
    "lineout_request": None,
    "lineout_last_restore_error": None,
}


def _ndi_supervisor_config() -> Dict[str, Any]:
    """Read reconnect/stall settings from the current config dict."""
    return {
        "enabled": bool(cfg.get("ndi_auto_reconnect_enabled", True)),
        "poll_s": max(0.25, float(cfg.get("ndi_supervisor_poll_s", 1.0))),
        "startup_grace_s": max(1.0, float(cfg.get("ndi_startup_grace_s", 10.0))),
        "stall_timeout_s": max(1.0, float(cfg.get("ndi_stall_timeout_s", 15.0))),
        "initial_backoff_s": max(0.25, float(cfg.get("ndi_reconnect_initial_backoff_s", 1.0))),
        "max_backoff_s": max(1.0, float(cfg.get("ndi_reconnect_max_backoff_s", 15.0))),
    }


def _ndi_req_to_dict(req: "StartReq") -> Dict[str, Any]:
    return {
        "channel_uuid": req.channel_uuid,
        "ndi_name": req.ndi_name,
        "profile": req.profile,
        "deinterlace": bool(req.deinterlace),
        "buffer_extra_ms": int(req.buffer_extra_ms),
        "ndi_qos": bool(req.ndi_qos),
        "ndi_multicast_enabled": bool(req.ndi_multicast_enabled),
        "ndi_multicast_addr": str(req.ndi_multicast_addr or ""),
        "ndi_multicast_ttl": int(req.ndi_multicast_ttl),
    }


def _lineout_req_to_dict(req: "LineOutStartReq") -> Dict[str, Any]:
    return {
        "device_id": req.device_id,
        "volume": float(req.volume),
    }


def _channel_summary_for_uuid(channel_uuid: Optional[str]) -> Dict[str, Any]:
    if not channel_uuid:
        return {}
    try:
        channels = tvh.list_channels(force_refresh=False)
    except Exception:
        return {}
    for channel in channels:
        if channel.get("uuid") == channel_uuid:
            return {
                "channel_name": channel.get("name"),
                "channel_number": channel.get("number"),
            }
    return {}


RF_STATUS_LOCK = threading.Lock()
RF_STATUS_CACHE: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
RF_STATUS_DEFAULT_TTL_S = 3.0


def _rf_status_cache_ttl_s() -> float:
    try:
        value = float(cfg.get("rf_status_ttl_s", RF_STATUS_DEFAULT_TTL_S))
    except Exception:
        value = RF_STATUS_DEFAULT_TTL_S
    return max(RF_STATUS_DEFAULT_TTL_S, min(120.0, value))


def _rf_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _rf_percent(value: Any) -> Optional[int]:
    n = _rf_number(value)
    if n is None:
        return None
    text = str(value or "")
    if "%" in text or 0 <= n <= 100:
        return max(0, min(100, int(round(n))))
    if 100 < n <= 65535:
        return max(0, min(100, int(round((n / 65535.0) * 100))))
    return None


def _rf_kind(percent: Optional[int], *, snr: Any = None) -> str:
    if percent is not None:
        if percent >= 65:
            return "good"
        if percent >= 35:
            return "warn"
        return "bad"
    snr_n = _rf_number(snr)
    if snr_n is not None and 0 <= snr_n <= 60:
        if snr_n >= 28:
            return "good"
        if snr_n >= 18:
            return "warn"
    return "bad"


def _rf_kind_from_dbm(dbm: Optional[float], percent: Optional[int], *, snr: Any = None) -> str:
    if dbm is not None:
        if dbm >= -65.0:
            return "good"
        if dbm >= -80.0:
            return "warn"
        return "bad"
    return _rf_kind(percent, snr=snr)


def _rf_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return str(value)


def _rf_dbm_from_signal(signal: Any, signal_percent: Optional[int]) -> Tuple[Optional[float], bool]:
    if signal in (None, ""):
        return None, False
    text = str(signal or "").strip().lower()
    n = _rf_number(signal)
    if n is None:
        return None, False

    if "mdbm" in text and -130000.0 <= n <= 20000.0:
        return round(n / 1000.0, 1), False
    if "dbm" in text and -130.0 <= n <= 20.0:
        return round(n, 1), False
    if -130.0 <= n < 0.0:
        return round(n, 1), False

    if signal_percent is not None:
        # TVHeadend often exposes DVB signal as a percentage/raw 0-65535 value.
        # DVB drivers do not all report calibrated power, so this is a conservative
        # display estimate for a cable-fed DVB-T/T2 receiver.
        return round(-95.0 + (signal_percent / 100.0) * 60.0, 1), True
    return None, False


def _rf_dbm_label(dbm: Optional[float], estimated: bool) -> str:
    if dbm is None:
        return "N/A"
    rounded = round(float(dbm), 1)
    if abs(rounded - round(rounded)) < 0.05:
        text = f"{int(round(rounded))} dBm"
    else:
        text = f"{rounded:.1f} dBm"
    return f"~{text}" if estimated else text


def _rf_scale_is_db(scale: Any) -> bool:
    text = str(scale or "").strip().lower()
    return text in ("2", "db", "dbm", "decibel", "decibels")


def _rf_scaled_db_value(value: Any, scale: Any) -> Optional[float]:
    if not _rf_scale_is_db(scale):
        return None
    n = _rf_number(value)
    if n is None:
        return None
    if abs(n) >= 1000:
        return round(n / 1000.0, 1)
    return round(n, 1)


def _rf_scaled_snr_text(snr: Any, snr_scale: Any) -> Optional[str]:
    snr_db = _rf_scaled_db_value(snr, snr_scale)
    if snr_db is not None:
        return f"{snr_db:.1f}".rstrip("0").rstrip(".") + " dB"
    return _rf_text(snr)


def _rf_dbm_from_signal_scaled(signal: Any, signal_scale: Any, signal_percent: Optional[int]) -> Tuple[Optional[float], bool]:
    signal_db = _rf_scaled_db_value(signal, signal_scale)
    if signal_db is not None:
        return signal_db, False
    return _rf_dbm_from_signal(signal, signal_percent)


def _rf_status_from_fields(
    *,
    signal: Any,
    snr: Any,
    signal_scale: Any = None,
    snr_scale: Any = None,
    mux_label: Optional[str] = None,
    source: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    signal_percent = None if _rf_scale_is_db(signal_scale) else _rf_percent(signal)
    snr_percent = None if _rf_scale_is_db(snr_scale) else _rf_percent(snr)
    percent = signal_percent if signal_percent is not None else snr_percent
    dbm, dbm_estimated = _rf_dbm_from_signal_scaled(signal, signal_scale, signal_percent)
    dbm_label = _rf_dbm_label(dbm, dbm_estimated)
    available = dbm is not None or percent is not None or signal not in (None, "") or snr not in (None, "")
    snr_for_kind = _rf_scaled_db_value(snr, snr_scale) if _rf_scale_is_db(snr_scale) else snr
    label = dbm_label if dbm is not None else (f"{percent}%" if percent is not None else (_rf_text(signal) or _rf_text(snr) or "N/A"))
    out = {
        "available": available,
        "kind": _rf_kind_from_dbm(dbm, percent, snr=snr_for_kind),
        "label": label,
        "dbm": dbm,
        "dbm_estimated": dbm_estimated,
        "dbm_label": dbm_label,
        "percent": percent,
        "signal": _rf_text(signal),
        "signal_percent": signal_percent,
        "snr": _rf_scaled_snr_text(snr, snr_scale),
        "snr_percent": snr_percent,
        "mux": mux_label,
        "source": source,
    }
    if extra:
        out.update(extra)
    return out


def _rf_status_from_mux(mux: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    return _rf_status_from_fields(
        signal=mux.get("signal"),
        snr=mux.get("snr"),
        mux_label=_mux_label(mux),
        source=source,
    )


def _service_matches_channel(service: Dict[str, Any], channel_uuid: Optional[str], channel_name: Optional[str]) -> bool:
    if channel_uuid:
        for key in ("channel_uuid", "channel", "channelid", "channel_id"):
            if str(service.get(key) or "").strip() == str(channel_uuid).strip():
                return True
    if channel_name:
        wanted = str(channel_name).strip().lower()
        for key in ("channelname", "channel_name", "name", "svcname"):
            value = str(service.get(key) or "").strip().lower()
            if value and value == wanted:
                return True
    return False


def _mux_matches_ref(mux: Dict[str, Any], ref: str) -> bool:
    ref_s = str(ref or "").strip()
    if not ref_s:
        return False
    candidates = (
        mux.get("uuid"),
        mux.get("name"),
        mux.get("muxname"),
        mux.get("multiplex"),
        mux.get("frequency"),
        mux.get("freq"),
    )
    return any(str(candidate or "").strip() == ref_s for candidate in candidates)


def _mux_for_service(muxes: List[Dict[str, Any]], service: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    refs = [
        service.get(key)
        for key in ("mux_uuid", "multiplex_uuid", "mux", "multiplex", "muxname", "network_mux_uuid")
        if service.get(key) not in (None, "")
    ]
    for ref in refs:
        for mux in muxes:
            if _mux_matches_ref(mux, str(ref)):
                return mux
    return None


def _rf_norm_ref(value: Any) -> str:
    return re.sub(r"[^a-z0-9.]+", "", str(value or "").strip().lower())


def _rf_freq_tokens(freq: Any) -> List[str]:
    freq_i = _coerce_int(freq)
    if freq_i is None or freq_i <= 0:
        return []
    mhz = freq_i / 1_000_000.0
    mhz_text = f"{mhz:.3f}".rstrip("0").rstrip(".")
    return [
        str(freq_i),
        _rf_norm_ref(f"{mhz_text}MHz"),
        _rf_norm_ref(f"{mhz_text} MHz"),
    ]


def _rf_input_matches_mux(input_status: Dict[str, Any], mux: Optional[Dict[str, Any]]) -> bool:
    if not mux:
        return False
    haystack = _rf_norm_ref(" ".join(str(input_status.get(key) or "") for key in ("stream", "input", "uuid")))
    candidates: List[str] = []
    for key in ("uuid", "name", "muxname", "multiplex", "frequency", "freq"):
        value = mux.get(key)
        if value not in (None, ""):
            candidates.append(_rf_norm_ref(value))
    candidates.extend(_rf_freq_tokens(mux.get("frequency") or mux.get("freq")))
    return any(candidate and candidate in haystack for candidate in candidates)


def _rf_input_matches_subscription(input_status: Dict[str, Any], subscription: Dict[str, Any]) -> bool:
    stream = _rf_norm_ref(input_status.get("stream"))
    service = _rf_norm_ref(subscription.get("service"))
    return bool(stream and service and stream in service)


def _rf_status_from_input(input_status: Dict[str, Any], *, mux: Optional[Dict[str, Any]], source: str) -> Dict[str, Any]:
    mux_label = _mux_label(mux) if mux else str(input_status.get("stream") or "").strip() or None
    return _rf_status_from_fields(
        signal=input_status.get("signal"),
        snr=input_status.get("snr"),
        signal_scale=input_status.get("signal_scale"),
        snr_scale=input_status.get("snr_scale"),
        mux_label=mux_label,
        source=source,
        extra={
            "input": input_status.get("input"),
            "stream": input_status.get("stream"),
            "ber": input_status.get("ber"),
            "unc": input_status.get("unc"),
            "cc": input_status.get("cc"),
            "bps": input_status.get("bps"),
        },
    )


def _live_rf_status_for_mux(mux: Optional[Dict[str, Any]], channel_name: Optional[str]) -> Optional[Dict[str, Any]]:
    try:
        inputs = tvh.status_inputs()
    except Exception:
        inputs = []
    if not inputs:
        return None

    for input_status in inputs:
        if _rf_input_matches_mux(input_status, mux):
            return _rf_status_from_input(input_status, mux=mux, source="active_input")

    if channel_name:
        wanted = str(channel_name or "").strip().lower()
        try:
            subscriptions = tvh.status_subscriptions()
        except Exception:
            subscriptions = []
        for subscription in subscriptions:
            sub_channel = str(subscription.get("channel") or "").strip().lower()
            sub_service = str(subscription.get("service") or "").strip().lower()
            if (wanted and (sub_channel == wanted or wanted in sub_service)):
                for input_status in inputs:
                    if _rf_input_matches_subscription(input_status, subscription):
                        return _rf_status_from_input(input_status, mux=mux, source="active_input")

    if mux is None and len(inputs) == 1:
        return _rf_status_from_input(inputs[0], mux=None, source="tuned_input")

    return None


def _best_rf_mux(muxes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for mux in muxes:
        signal_percent = _rf_percent(mux.get("signal"))
        snr_percent = _rf_percent(mux.get("snr"))
        if signal_percent is None and snr_percent is None and mux.get("signal") in (None, "") and mux.get("snr") in (None, ""):
            continue
        score = float(signal_percent if signal_percent is not None else (snr_percent if snr_percent is not None else 0))
        if (_coerce_int(mux.get("num_svc")) or 0) > 0:
            score += 5.0
        if best is None or score > best[0]:
            best = (score, mux)
    return best[1] if best else None


def _rf_unavailable() -> Dict[str, Any]:
    return {
        "available": False,
        "kind": "bad",
        "label": "N/A",
        "dbm": None,
        "dbm_estimated": False,
        "dbm_label": "N/A",
        "percent": None,
        "signal": None,
        "signal_percent": None,
        "snr": None,
        "snr_percent": None,
        "mux": None,
        "source": "unavailable",
    }


def _rf_status_for_channel_uncached(channel_uuid: Optional[str] = None, channel_name: Optional[str] = None) -> Dict[str, Any]:
    unavailable = {
        **_rf_unavailable(),
    }
    try:
        network = _resolve_dvbt_network()
        network_uuid = str(network.get("uuid") or "")
        muxes = tvh.list_muxes_for_network(network_uuid) if network_uuid else []
    except Exception as e:
        out = dict(unavailable)
        out["error"] = str(e)
        return out

    matched_service: Optional[Dict[str, Any]] = None
    if channel_uuid or channel_name:
        try:
            for service in tvh.list_services(hidemode="none"):
                if _service_matches_channel(service, channel_uuid, channel_name):
                    matched_service = service
                    break
        except Exception:
            matched_service = None

    if matched_service:
        service_rf = _rf_status_from_mux(matched_service, source="service")
        if service_rf.get("available"):
            return service_rf
        mux = _mux_for_service(muxes, matched_service)
        if mux:
            live_rf = _live_rf_status_for_mux(mux, channel_name)
            if live_rf and live_rf.get("available"):
                return live_rf
            return _rf_status_from_mux(mux, source="active_mux")

    live_rf = _live_rf_status_for_mux(None, channel_name)
    if live_rf and live_rf.get("available"):
        return live_rf

    mux = _best_rf_mux(muxes)
    if mux:
        return _rf_status_from_mux(mux, source="best_mux")
    return unavailable


def _rf_status_for_channel(channel_uuid: Optional[str] = None, channel_name: Optional[str] = None) -> Dict[str, Any]:
    ttl = _rf_status_cache_ttl_s()
    key = (
        str(cfg.get("tvh_base_url") or ""),
        str(cfg.get("tvh_dvbt_network_uuid") or cfg.get("tvh_dvbt_network_name") or ""),
        str(channel_uuid or ""),
        str(channel_name or "").strip().lower(),
    )

    with RF_STATUS_LOCK:
        cached = RF_STATUS_CACHE.get(key)
        now = time.monotonic()
        if cached and (now - float(cached.get("monotonic_at") or 0.0)) < ttl:
            out = deepcopy(cached.get("value") or _rf_unavailable())
            out["cached"] = True
            out["cache_ttl_s"] = ttl
            return out

    # Tvheadend calls can take seconds during a restart or retune. Do not hold the
    # cache lock while performing network I/O; unrelated status requests should
    # remain responsive and can safely converge on the same refreshed value.
    value = _rf_status_for_channel_uncached(channel_uuid=channel_uuid, channel_name=channel_name)
    with RF_STATUS_LOCK:
        value["cached"] = False
        value["cache_ttl_s"] = ttl
        value["last_updated_at"] = int(time.time())
        RF_STATUS_CACHE[key] = {
            "monotonic_at": time.monotonic(),
            "value": deepcopy(value),
        }
        if len(RF_STATUS_CACHE) > 32:
            oldest = sorted(RF_STATUS_CACHE.items(), key=lambda item: float(item[1].get("monotonic_at") or 0.0))[:8]
            for old_key, _ in oldest:
                RF_STATUS_CACHE.pop(old_key, None)
        return deepcopy(value)


def _restore_desired_lineout(reason: str = "supervisor restore") -> None:
    with NDI_SUPERVISOR_LOCK:
        audio_req = deepcopy(NDI_SUPERVISOR_STATE.get("lineout_request"))
        desired = bool(NDI_SUPERVISOR_STATE.get("lineout_desired"))
    if not desired or not audio_req:
        return
    try:
        current = ndi_bridge.lineout_status(include_logs=False)
        if current.get("running"):
            return
    except Exception:
        pass
    try:
        ndi_bridge.lineout_start(**audio_req)
        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE["lineout_last_restore_error"] = None
        ndi_bridge._push_log(f"Line output restored after NDI restart: {reason}")
    except Exception as e:
        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE["lineout_last_restore_error"] = str(e)


def _start_ndi_pipeline_from_dict(req_d: Dict[str, Any], *, reason: str, force_refresh: bool = False) -> str:
    """Resolve a tvheadend URL and start/restart the NDI pipeline."""
    stream_url = tvh.get_stream_url_for_uuid(req_d["channel_uuid"], profile=req_d["profile"], force_refresh=force_refresh)
    channel_summary = _channel_summary_for_uuid(req_d.get("channel_uuid"))
    req_d.update(channel_summary)
    with NDI_SUPERVISOR_LOCK:
        current_request = NDI_SUPERVISOR_STATE.get("request")
        if isinstance(current_request, dict) and current_request.get("channel_uuid") == req_d.get("channel_uuid"):
            current_request.update(channel_summary)
        NDI_SUPERVISOR_STATE["last_start_attempt_at"] = time.time()
        NDI_SUPERVISOR_STATE["last_restart_reason"] = reason
        NDI_SUPERVISOR_STATE["last_stream_url"] = stream_url
        NDI_SUPERVISOR_STATE["last_error"] = None
        NDI_SUPERVISOR_STATE["last_rendered"] = None
        NDI_SUPERVISOR_STATE["last_rendered_change_at"] = time.time()
        NDI_SUPERVISOR_STATE["pipeline_status"] = "starting"
        NDI_SUPERVISOR_STATE["healthy_since"] = None

    ndi_bridge.start_with_delay(
        input_url=stream_url,
        ndi_name=req_d["ndi_name"],
        channel_uuid=req_d["channel_uuid"],
        delay_ms=NDI_DELAY_DEFAULT_MS,
        deinterlace=req_d["deinterlace"],
        buffer_extra_ms=req_d["buffer_extra_ms"],
        ndi_qos=req_d["ndi_qos"],
        ndi_multicast_enabled=req_d["ndi_multicast_enabled"],
        ndi_multicast_addr=req_d["ndi_multicast_addr"],
        ndi_multicast_ttl=req_d["ndi_multicast_ttl"],
    )
    return stream_url


def _restart_ndi_pipeline(reason: str) -> None:
    with NDI_SUPERVISOR_LOCK:
        req_d = deepcopy(NDI_SUPERVISOR_STATE.get("request"))
        if not NDI_SUPERVISOR_STATE.get("desired") or not req_d:
            return
        NDI_SUPERVISOR_STATE["restart_count"] = int(NDI_SUPERVISOR_STATE.get("restart_count") or 0) + 1
        NDI_SUPERVISOR_STATE["last_restart_reason"] = reason
    try:
        _start_ndi_pipeline_from_dict(req_d, reason=reason, force_refresh=True)
        ndi_bridge._push_log(f"Supervisor restart requested: {reason}")
    except Exception as e:
        # Leave desired=True so the supervisor keeps trying with backoff.
        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE["last_error"] = str(e)
            NDI_SUPERVISOR_STATE["last_start_attempt_at"] = time.time()
            NDI_SUPERVISOR_STATE["pipeline_status"] = "failed"
        try:
            ndi_bridge._push_err(f"Supervisor restart failed: {e}")
        except Exception:
            pass


def _ndi_supervisor_loop() -> None:
    while not NDI_SUPERVISOR_STOP.is_set():
        cfg_s = _ndi_supervisor_config()
        if NDI_SUPERVISOR_STOP.wait(cfg_s["poll_s"]):
            break
        if not cfg_s["enabled"]:
            continue

        with NDI_SUPERVISOR_LOCK:
            desired = bool(NDI_SUPERVISOR_STATE.get("desired"))
            req_d = deepcopy(NDI_SUPERVISOR_STATE.get("request"))
            last_attempt = NDI_SUPERVISOR_STATE.get("last_start_attempt_at") or 0
            restart_count = int(NDI_SUPERVISOR_STATE.get("restart_count") or 0)
            was_running = bool(NDI_SUPERVISOR_STATE.get("was_running"))
        if not desired or not req_d:
            continue

        try:
            st = ndi_bridge.status_lite(include_logs=False, include_stats=True)
        except Exception as e:
            with NDI_SUPERVISOR_LOCK:
                NDI_SUPERVISOR_STATE["last_error"] = f"status failed: {e}"
            continue

        now = time.time()
        running = bool(st.get("running"))
        if running:
            rendered = st.get("ndi_rendered")
            stats_available = bool(st.get("ndi_stats_available"))
            try:
                rendered_i = int(rendered) if rendered is not None else None
            except Exception:
                rendered_i = None

            should_restart = False
            restart_reason = ""
            with NDI_SUPERVISOR_LOCK:
                NDI_SUPERVISOR_STATE["was_running"] = True
                NDI_SUPERVISOR_STATE["pipeline_status"] = "running"
                prev = NDI_SUPERVISOR_STATE.get("last_rendered")
                last_change = NDI_SUPERVISOR_STATE.get("last_rendered_change_at") or now
                started_at = st.get("started_at") or NDI_SUPERVISOR_STATE.get("last_start_attempt_at") or now

                if stats_available and rendered_i is not None:
                    try:
                        prev_i = int(prev) if prev is not None else None
                    except Exception:
                        prev_i = None

                    if prev_i is None or rendered_i != prev_i:
                        NDI_SUPERVISOR_STATE["last_rendered"] = rendered_i
                        if rendered_i > 0:
                            NDI_SUPERVISOR_STATE["last_rendered_change_at"] = now
                            last_change = now
                            if NDI_SUPERVISOR_STATE.get("healthy_since") is None:
                                NDI_SUPERVISOR_STATE["healthy_since"] = now
                            if NDI_SUPERVISOR_STATE.get("last_success_at") is None:
                                NDI_SUPERVISOR_STATE["last_success_at"] = now

                    healthy_since = NDI_SUPERVISOR_STATE.get("healthy_since")
                    if healthy_since:
                        if (now - float(last_change)) >= cfg_s["stall_timeout_s"]:
                            should_restart = True
                            restart_reason = f"stall: no NDI frames rendered for {cfg_s['stall_timeout_s']:.1f}s"
                    else:
                        first_frame_timeout_s = max(cfg_s["startup_grace_s"], cfg_s["stall_timeout_s"] * 2.0)
                        if (now - float(started_at)) >= first_frame_timeout_s:
                            should_restart = True
                            restart_reason = f"startup: no NDI frames rendered for {first_frame_timeout_s:.1f}s"
                else:
                    # Some ndisink builds do not expose stats. In that case avoid false stall
                    # restarts and mark the pipeline healthy once it survives startup grace.
                    if (now - float(started_at)) >= cfg_s["startup_grace_s"]:
                        if NDI_SUPERVISOR_STATE.get("healthy_since") is None:
                            NDI_SUPERVISOR_STATE["healthy_since"] = now
                        if NDI_SUPERVISOR_STATE.get("last_success_at") is None:
                            NDI_SUPERVISOR_STATE["last_success_at"] = now

                healthy_since = NDI_SUPERVISOR_STATE.get("healthy_since")
                if healthy_since and (now - float(healthy_since)) >= 60 and int(NDI_SUPERVISOR_STATE.get("restart_count") or 0) != 0:
                    NDI_SUPERVISOR_STATE["restart_count"] = 0

            if should_restart:
                _restart_ndi_pipeline(restart_reason)
                continue

            _restore_desired_lineout("NDI pipeline healthy")
            continue

        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE["pipeline_status"] = "stopped"

        # Pipeline is not running while the user still wants it. Reconnect after backoff.
        if not was_running and (now - float(last_attempt)) < cfg_s["startup_grace_s"]:
            continue
        backoff = min(cfg_s["max_backoff_s"], cfg_s["initial_backoff_s"] * (2 ** min(restart_count, 5)))
        if (now - float(last_attempt)) >= backoff:
            _restart_ndi_pipeline("pipeline stopped unexpectedly")


TV_SETUP_STATE: Dict[str, Any] = {
    "running": False,
    "done": False,
    "partial": False,
    "percent": 0,
    "step": "Idle",
    "logs": [],
    "error": None,
    "started_at": None,
    "finished_at": None,
    "selected_scanfile": None,
    "scan_note": None,
}
TV_SETUP_LOCK = threading.Lock()

def _tv_setup_snapshot() -> Dict[str, Any]:
    with TV_SETUP_LOCK:
        return dict(TV_SETUP_STATE)

def _tv_setup_set(**patch: Any) -> None:
    with TV_SETUP_LOCK:
        TV_SETUP_STATE.update(patch)

def _tv_setup_log(message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    with TV_SETUP_LOCK:
        logs = list(TV_SETUP_STATE.get("logs", []))
        logs.append(f"[{ts}] {message}")
        TV_SETUP_STATE["logs"] = logs[-300:]

def _preferred_dvbt_scanfile(regions: List[Dict[str, Any]], configured: str = "") -> str:
    valid = {str(r.get("key") or "").strip() for r in regions}
    configured = str(configured or "").strip()

    def norm(value: Any) -> str:
        text = str(value or "").strip().lower()
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    def is_auto_default(value: Any) -> bool:
        normalized = norm(value)
        return "auto-defaul" in normalized

    if configured and configured in valid:
        return configured

    # Tvheadend scanfile keys vary by release and can be truncated by its API
    # (for example, dvb-t_auto-Defaul). Prefer the stable human-readable Generic
    # label and use the key only as a fallback signal.
    for region in regions:
        key = str(region.get("key") or "").strip()
        val = str(region.get("val") or "").strip()
        val_norm = norm(val)
        key_norm = norm(key)
        if ("generic" in val_norm and is_auto_default(val)) or (
            "dvb-t-auto" in key_norm and is_auto_default(key)
        ):
            return key

    if TELETOOL_UK_AUTO_SCANFILE in valid:
        return TELETOOL_UK_AUTO_SCANFILE

    for region in regions:
        key = str(region.get("key") or "").strip()
        val = str(region.get("val") or "").strip()
        if is_auto_default(key) or is_auto_default(val):
            return key

    return configured

def _resolve_dvbt_network() -> Dict[str, Any]:
    want_uuid = str(cfg.get("tvh_dvbt_network_uuid") or "").strip()
    want_name = str(cfg.get("tvh_dvbt_network_name") or "").strip().lower()
    networks = tvh.list_networks()
    if want_uuid:
        for net in networks:
            if str(net.get("uuid") or "") == want_uuid:
                return net
        raise RuntimeError(f"Configured DVB-T network uuid not found: {want_uuid}")
    if want_name:
        for net in networks:
            if str(net.get("name") or "").strip().lower() == want_name:
                return net
        raise RuntimeError(f"Configured DVB-T network name not found: {want_name}")
    for net in networks:
        combined = " ".join(str(net.get(k) or "") for k in ("name", "networkname", "scanfile", "class")).lower()
        if "dvb-t" in combined or "dvbt" in combined or "terrestrial" in combined:
            return net
    if len(networks) == 1:
        return networks[0]
    raise RuntimeError("Could not determine the DVB-T network. Set tvh_dvbt_network_uuid or tvh_dvbt_network_name in config.json.")

def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _mux_label(mux: Dict[str, Any]) -> str:
    name = str(mux.get("name") or mux.get("muxname") or "").strip()
    freq = _coerce_int(mux.get("frequency") or mux.get("freq"))
    parts: List[str] = []
    if name:
        parts.append(name)
    if freq:
        if freq >= 1_000_000:
            parts.append(f"{freq / 1_000_000:.3f} MHz")
        elif freq >= 1_000:
            parts.append(f"{freq / 1_000:.0f} kHz")
        else:
            parts.append(str(freq))
    if not parts:
        uuid = str(mux.get("uuid") or "unknown")
        return f"mux {uuid[:8]}"
    return " / ".join(parts)


def _scan_state_label(mux: Dict[str, Any]) -> str:
    for key in ("scan_result", "scan_status", "status"):
        value = mux.get(key)
        if value not in (None, ""):
            return str(value)
    state = _coerce_int(mux.get("scan_state"))
    return f"scan_state={state if state is not None else '?'}"


def _log_mux_diagnostics(muxes: List[Dict[str, Any]], *, prefix: str = "Mux") -> None:
    if not muxes:
        _tv_setup_log(f"{prefix}: no muxes were returned for the selected network.")
        return
    for mux in muxes:
        label = _mux_label(mux)
        scan_state = _scan_state_label(mux)
        num_svc = _coerce_int(mux.get("num_svc"))
        pids = _coerce_int(mux.get("num_pmt"))
        sig = mux.get("signal")
        snr = mux.get("snr")
        ber = mux.get("ber")
        unc = mux.get("unc")
        extra: List[str] = [scan_state]
        if num_svc is not None:
            extra.append(f"services={num_svc}")
        if pids is not None:
            extra.append(f"pmts={pids}")
        if sig not in (None, ""):
            extra.append(f"signal={sig}")
        if snr not in (None, ""):
            extra.append(f"snr={snr}")
        if ber not in (None, ""):
            extra.append(f"ber={ber}")
        if unc not in (None, ""):
            extra.append(f"unc={unc}")
        _tv_setup_log(f"{prefix}: {label} -> " + ", ".join(extra))


def _config_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(float(cfg.get(name, default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _mux_is_active(mux: Dict[str, Any]) -> bool:
    scan_state = _coerce_int(mux.get("scan_state"))
    if scan_state is not None:
        return scan_state != 0
    text = " ".join(str(mux.get(k) or "") for k in ("scan_result", "scan_status", "status")).lower()
    return any(word in text for word in ("active", "pending", "queued", "scanning"))


def _scan_progress_key(muxes: List[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
    key: List[Tuple[Any, ...]] = []
    for mux in muxes:
        scan_state = _coerce_int(mux.get("scan_state"))
        num_svc = _coerce_int(mux.get("num_svc"))
        num_pmt = _coerce_int(mux.get("num_pmt"))
        key.append((
            str(mux.get("uuid") or ""),
            str(mux.get("frequency") or mux.get("freq") or ""),
            scan_state if scan_state is not None else -1,
            num_svc if num_svc is not None else -1,
            num_pmt if num_pmt is not None else -1,
            str(mux.get("scan_result") or mux.get("scan_status") or mux.get("status") or ""),
        ))
    return tuple(sorted(key))


def _scan_mux_summary(muxes: List[Dict[str, Any]]) -> Dict[str, int]:
    active = sum(1 for mux in muxes if _mux_is_active(mux))
    total_services = sum(_coerce_int(mux.get("num_svc")) or 0 for mux in muxes)
    return {
        "muxes": len(muxes),
        "active": active,
        "complete": max(0, len(muxes) - active),
        "services": total_services,
    }


def _scan_setup_percent(summary: Dict[str, int], start: float = 20, end: float = 80) -> float:
    total = max(0, summary.get("muxes", 0))
    complete = max(0, min(total, summary.get("complete", 0)))
    if total == 0:
        return start
    return round(start + ((end - start) * complete / total), 1)


def _wait_for_scan(network_uuid: str, timeout_s: int = 600, stall_timeout_s: int = 120) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    deadline = time.time() + timeout_s
    stable = 0
    last_progress_key = None
    last_progress_at = time.time()
    diag_every = 0
    last_muxes: List[Dict[str, Any]] = []
    while time.time() < deadline:
        now = time.time()
        muxes = tvh.list_muxes_for_network(network_uuid)
        last_muxes = muxes
        summary = _scan_mux_summary(muxes)
        scan_step = "Scanning muxes and discovering services…"
        if summary["muxes"]:
            scan_step += f" {summary['complete']}/{summary['muxes']} complete"
        _tv_setup_set(percent=_scan_setup_percent(summary), step=scan_step)
        progress_key = _scan_progress_key(muxes)
        if progress_key != last_progress_key:
            _tv_setup_log(
                f"Scan progress: muxes={summary['muxes']} active={summary['active']} "
                f"complete={summary['complete']} services={summary['services']}"
            )
            last_progress_key = progress_key
            last_progress_at = now
            diag_every += 1
            if diag_every >= 3 or (muxes and summary["active"] == 0):
                _log_mux_diagnostics(muxes, prefix="Mux status")
                diag_every = 0
        if muxes and summary["active"] == 0:
            stable += 1
            if stable >= 3:
                return muxes, True, None
        else:
            stable = 0

        idle_s = now - last_progress_at
        if muxes and summary["active"] > 0 and idle_s >= stall_timeout_s:
            note = (
                f"Scan stalled after {int(idle_s)} seconds without mux progress "
                f"({summary['active']} active mux(es), {summary['services']} service(s) found)."
            )
            _tv_setup_log(note)
            _log_mux_diagnostics(muxes, prefix="Mux at stall")
            return muxes, False, note

        time.sleep(3)
    muxes = last_muxes or tvh.list_muxes_for_network(network_uuid)
    summary = _scan_mux_summary(muxes)
    note = (
        f"Scan timed out after {timeout_s} seconds "
        f"({summary['active']} active mux(es), {summary['services']} service(s) found)."
    )
    _tv_setup_log(note)
    _log_mux_diagnostics(muxes, prefix="Mux at timeout")
    return muxes, False, note

def _mapper_counts(status: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    total = _coerce_int(status.get("total")) or 0
    ok = _coerce_int(status.get("ok")) or 0
    fail = _coerce_int(status.get("fail")) or 0
    ignore = _coerce_int(status.get("ignore")) or 0
    done = ok + fail + ignore
    return total, done, ok, ignore, fail


def _wait_for_mapper(timeout_s: int = 300, expected_total: Optional[int] = None) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last_summary = None
    waiting_logged = False
    unexpected_total_logged: Optional[int] = None
    complete_mismatch_since: Optional[float] = None
    while time.time() < deadline:
        now = time.time()
        status = tvh.mapper_status()
        total, done, ok, ignore, fail = _mapper_counts(status)
        summary = (total, done, ok, ignore, fail)
        if summary != last_summary:
            _tv_setup_log(f"Mapper progress: {done}/{total} processed (ok={ok}, ignore={ignore}, fail={fail})")
            last_summary = summary
        expected_matches = expected_total is None or total == expected_total
        if total > 0 and done >= total and expected_matches:
            return status
        if total == 0 and expected_total:
            if not waiting_logged:
                _tv_setup_log(f"Waiting for TV service mapper to queue {expected_total} service(s).")
                waiting_logged = True
        elif expected_total and total != expected_total:
            if total != unexpected_total_logged:
                _tv_setup_log(f"TV service mapper queued {total} service(s); expected {expected_total}.")
                unexpected_total_logged = total
            if total > 0 and done >= total:
                if complete_mismatch_since is None:
                    complete_mismatch_since = now
                elif now - complete_mismatch_since >= 20:
                    _tv_setup_log(
                        "TV service mapper status still shows a different completed total; "
                        "continuing to channel refresh."
                    )
                    return status
            else:
                complete_mismatch_since = None
        else:
            complete_mismatch_since = None
        time.sleep(2)
    raise RuntimeError("Timed out waiting for TV service mapper")


def _wait_for_mapped_channels(min_count: int = 1, timeout_s: int = 60) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout_s
    last_count: Optional[int] = None
    channels: List[Dict[str, Any]] = []
    while time.time() < deadline:
        channels = tvh.list_channels(force_refresh=True)
        count = len(channels)
        if count != last_count:
            _tv_setup_log(f"Channel refresh: {count} mapped channel(s) visible.")
            last_count = count
        if count >= min_count:
            return channels
        time.sleep(2)
    return tvh.list_channels(force_refresh=True)


def _run_tv_setup_worker(scanfile_key: Optional[str] = None) -> None:
    try:
        scanfile_key = str(scanfile_key or cfg.get("tvh_dvbt_scanfile") or "").strip() or None
        if not scanfile_key:
            scanfile_key = _preferred_dvbt_scanfile(tvh.list_dvb_scanfiles("dvb-t"), "") or None
        _tv_setup_set(
            running=True,
            done=False,
            partial=False,
            percent=2,
            step="Stopping NDI before TV Setup…",
            error=None,
            selected_scanfile=scanfile_key,
            scan_note=None,
        )
        if _stop_ndi_for_tv_setup():
            _tv_setup_log("Stopped active NDI/audio pipeline before TV Setup.")
        else:
            _tv_setup_log("Confirmed NDI pipeline is stopped before TV Setup.")
        _tv_setup_set(percent=4, step="Loading current TV data…")
        if scanfile_key:
            _tv_setup_log(f"Selected predefined DVB-T/T2 mux region: {scanfile_key}")
        channels = tvh.list_channels(force_refresh=True)
        _tv_setup_log(f"Found {len(channels)} existing channel(s).")

        _tv_setup_set(percent=7, step="Deleting channels…")
        tvh.delete_channels([c["uuid"] for c in channels if c.get("uuid")])
        time.sleep(1)
        channels_after = tvh.list_channels(force_refresh=True)
        _tv_setup_log(f"Channels remaining after delete: {len(channels_after)}")

        _tv_setup_set(percent=10, step="Loading existing services…")
        services = tvh.list_services(hidemode="none")
        _tv_setup_log(f"Found {len(services)} existing service(s).")

        _tv_setup_set(percent=13, step="Deleting services…")
        tvh.delete_services([s["uuid"] for s in services if s.get("uuid")])
        time.sleep(1)
        services_after = tvh.list_services(hidemode="none")
        _tv_setup_log(f"Services remaining after delete: {len(services_after)}")

        _tv_setup_set(percent=16, step="Resolving DVB-T network…")
        network = _resolve_dvbt_network()
        network_uuid = str(network.get("uuid") or "")
        network_name = str(network.get("name") or network_uuid)
        if not network_uuid:
            raise RuntimeError("Resolved DVB-T network is missing a uuid")
        _tv_setup_log(f"Using DVB-T network: {network_name} ({network_uuid})")

        if scanfile_key:
            _tv_setup_set(percent=18, step="Applying selected predefined muxes…")
            mux_result = tvh.replace_muxes_from_scanfile(network_uuid, scanfile_key)
            _tv_setup_log(
                f"Applied predefined muxes: deleted {mux_result.get('deleted', 0)} existing mux(es), "
                f"created {mux_result.get('created', 0)} mux(es)."
            )
            for err in mux_result.get("errors", []):
                _tv_setup_log(f"Mux create warning: {err}")
            _update_config({"tvh_dvbt_scanfile": scanfile_key})
        else:
            _tv_setup_log("No predefined mux region selected; scanning the existing mux list.")

        _tv_setup_set(percent=19, step="Starting DVB-T scan…")
        tvh.scan_network(network_uuid)
        _tv_setup_log("Requested DVB-T network scan.")

        _tv_setup_set(percent=20, step="Scanning muxes and discovering services…")
        scan_timeout_s = _config_int("tvh_scan_timeout_s", 600, min_value=60, max_value=3600)
        scan_stall_s = _config_int("tvh_scan_stall_timeout_s", 120, min_value=30, max_value=900)
        muxes, scan_complete, scan_note = _wait_for_scan(network_uuid, timeout_s=scan_timeout_s, stall_timeout_s=scan_stall_s)
        if scan_complete:
            _tv_setup_log(f"Scan finished across {len(muxes)} mux(es).")
        else:
            _tv_setup_log(f"{scan_note} Checking discovered services before deciding setup result.")

        _tv_setup_set(percent=82, step="Loading discovered services…")
        scanned_services = tvh.list_services(hidemode="none")
        service_uuids = [s.get("uuid") for s in scanned_services if s.get("uuid")]
        _tv_setup_log(f"Discovered {len(service_uuids)} service(s) available for mapping.")
        if scanned_services:
            preview = ", ".join(str(s.get("svcname") or s.get("name") or s.get("channelname") or s.get("uuid")) for s in scanned_services[:10])
            if preview:
                _tv_setup_log(f"Service preview: {preview}")
        if not service_uuids:
            _tv_setup_log("No services were returned immediately after scan; waiting 10 seconds and checking again.")
            time.sleep(10)
            scanned_services = tvh.list_services(hidemode="none")
            service_uuids = [s.get("uuid") for s in scanned_services if s.get("uuid")]
            _tv_setup_log(f"Second service check found {len(service_uuids)} service(s).")
        if not service_uuids:
            _tv_setup_log("Detailed mux results after scan:")
            _log_mux_diagnostics(muxes, prefix="Mux result")
            if scan_complete:
                raise RuntimeError("No services were discovered after the DVB-T scan")
            raise RuntimeError(f"{scan_note} No services were discovered, so TV Setup cannot map channels.")

        if not scan_complete:
            _tv_setup_log("Continuing with partial setup because discovered services are available to map.")

        _tv_setup_set(percent=90, step="Mapping services to channels…")
        tvh.map_services(service_uuids)
        mapper_status = _wait_for_mapper(expected_total=len(service_uuids))
        mapper_total, _mapper_done, mapper_ok, mapper_ignore, mapper_fail = _mapper_counts(mapper_status)
        _tv_setup_log(
            f"Mapper complete: total={mapper_total}, ok={mapper_ok}, "
            f"ignore={mapper_ignore}, fail={mapper_fail}."
        )

        _tv_setup_set(percent=97, step="Refreshing channel list…")
        mapped_channels = _wait_for_mapped_channels(min_count=1 if mapper_ok > 0 else 0)
        _tv_setup_log(f"Mapped {len(mapped_channels)} channel(s).")
        if mapper_ok > 0 and not mapped_channels:
            raise RuntimeError(
                "TV service mapper reported mapped services, but no channels appeared in the channel list"
            )

        if scan_complete:
            _tv_setup_set(
                running=False,
                done=True,
                partial=False,
                percent=100,
                step="TV Setup complete",
                scan_note=None,
                finished_at=int(time.time()),
            )
        else:
            _tv_setup_set(
                running=False,
                done=True,
                partial=True,
                percent=100,
                step="TV Setup partially complete",
                scan_note=scan_note,
                finished_at=int(time.time()),
            )
    except Exception as e:
        _tv_setup_log(f"ERROR: {e}")
        _tv_setup_set(
            running=False,
            done=True,
            partial=False,
            percent=100,
            step="TV Setup failed",
            error=str(e),
            finished_at=int(time.time()),
        )

@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    global cfg, tvh, ndi_bridge, _active_profile, NDI_DELAY_DEFAULT_MS
    global NDI_SUPERVISOR_THREAD

    cfg = _load_config()
    tvh = _build_tvh_client(cfg)
    ndi_bridge = GstNDIBridge(config=cfg)
    _active_profile = str(cfg.get("tvh_stream_profile", "pass"))
    NDI_DELAY_DEFAULT_MS = int(cfg.get("ndi_delay_ms", 250))

    NDI_SUPERVISOR_STOP.clear()
    NDI_SUPERVISOR_THREAD = threading.Thread(
        target=_ndi_supervisor_loop,
        name="ndi-supervisor",
        daemon=True,
    )
    NDI_SUPERVISOR_THREAD.start()
    fleet_manager.startup()

    try:
        yield
    finally:
        NDI_SUPERVISOR_STOP.set()
        if NDI_SUPERVISOR_THREAD is not None and NDI_SUPERVISOR_THREAD.is_alive():
            NDI_SUPERVISOR_THREAD.join(timeout=3.0)
        NDI_SUPERVISOR_THREAD = None

        fleet_manager.shutdown()
        try:
            ndi_bridge.lineout_stop()
        except Exception:
            pass
        try:
            ndi_bridge.stop()
        except Exception:
            pass
        try:
            tvh.close()
        except Exception:
            pass


app = FastAPI(title="TV to NDI/Line Audio Bridge", lifespan=_app_lifespan)
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)

NDI_GATED_UI_PATHS = {
    "/",
    "/audio",
    "/system",
    "/manager",
    "/static/index.html",
    "/static/audio.html",
    "/static/system.html",
    "/static/manager.html",
}


@app.middleware("http")
async def require_ndi_runtime_for_ui(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if request.method in {"GET", "HEAD"} and path in NDI_GATED_UI_PATHS:
        if not _ndi_runtime_status()["ready"]:
            return RedirectResponse(
                url="/ndi-setup",
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
    return await call_next(request)


app.mount("/static", StaticFiles(directory=static_dir), name="static")
# ---------------- Static pages ----------------
def _ensure_static_pages():
    """
    This app historically serves pages from ./static.
    To avoid surprises (and to make local dev easier), we also copy any root-level
    *.html into ./static if the static copy is missing.
    """
    for name in ("index.html", "audio.html", "system.html", "manager.html", "ndi-setup.html", "common.css", "common.js"):
        dst = static_dir / name
        if dst.exists():
            continue
        src = BASE_DIR / name
        if src.exists():
            try:
                shutil.copyfile(src, dst)
            except Exception:
                # Non-fatal: the user can still place the files manually.
                pass
_ensure_static_pages()


@app.get("/ndi-setup")
def ndi_setup_page():
    status = _ndi_runtime_status()
    if status["ready"]:
        return RedirectResponse(url="/", status_code=302)

    page = static_dir / "ndi-setup.html"
    if not page.exists():
        raise HTTPException(500, "static/ndi-setup.html missing")

    if status["installed"] and not status["verified"]:
        stage_class = "warn"
        stage_label = "Runtime installed but verification is required"
    elif status["staged"]:
        stage_class = "warn"
        stage_label = "Runtime file detected; installation is required"
    else:
        stage_class = "bad"
        stage_label = "Runtime file not detected"
    upload_disabled = "" if status["upload_enabled"] else "disabled"
    upload_hint = (
        "Drop the ARM64 NDI runtime here; TeleTool will validate and install it automatically."
        if status["upload_enabled"]
        else "The privileged installer helper is unavailable. Rerun the full TeleTool setup once, then refresh this page."
    )
    replacements = {
        "{{STAGE_CLASS}}": stage_class,
        "{{STAGE_LABEL}}": html.escape(stage_label),
        "{{SDK_URL}}": html.escape(str(status["sdk_url"]), quote=True),
        "{{RUNTIME_NAME}}": html.escape(str(status["runtime_name"])),
        "{{DROP_PATH}}": html.escape(str(status["drop_path"])),
        "{{DROP_DIRECTORY}}": html.escape(str(status["drop_directory"])),
        "{{SETUP_COMMAND}}": html.escape(str(status["setup_command"])),
        "{{UPLOAD_DISABLED}}": upload_disabled,
        "{{UPLOAD_HINT}}": html.escape(upload_hint),
        "{{UPLOAD_MAX_MIB}}": str(int(status["upload_max_bytes"]) // (1024 * 1024)),
    }
    content = page.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        content = content.replace(marker, value)
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@app.get("/")
def root():
    index = static_dir / "index.html"
    if not index.exists():
        raise HTTPException(500, "static/index.html missing")
    return FileResponse(str(index))
@app.get("/audio")
def audio_page():
    page = static_dir / "audio.html"
    if not page.exists():
        raise HTTPException(500, "static/audio.html missing")
    return FileResponse(str(page))
@app.get("/system")
def system_page():
    page = static_dir / "system.html"
    if not page.exists():
        raise HTTPException(500, "static/system.html missing")
    return FileResponse(str(page))
@app.get("/manager")
def manager_page():
    page = static_dir / "manager.html"
    if not page.exists():
        raise HTTPException(500, "static/manager.html missing")
    return FileResponse(str(page))
# ---------------- Existing API ----------------


@app.get("/api/ndi/runtime")
def api_ndi_runtime():
    return _ndi_runtime_status()


@app.post("/api/ndi/runtime/upload")
async def api_upload_ndi_runtime(request: Request):
    if _ndi_runtime_status()["ready"]:
        raise HTTPException(409, "The NDI SDK runtime is already installed.")
    if not (NDI_INSTALL_HELPER.is_file() and os.access(NDI_INSTALL_HELPER, os.X_OK)):
        raise HTTPException(503, "The NDI runtime installer helper is not installed. Rerun the full TeleTool setup.")
    if not NDI_UPLOAD_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another NDI runtime upload is already in progress.")

    upload_tmp = NDI_DROP_PATH.with_name(f".{NDI_RUNTIME_NAME}.upload-{uuid.uuid4().hex}")
    total = 0
    try:
        content_length = str(request.headers.get("content-length") or "").strip()
        if content_length:
            try:
                if int(content_length) > NDI_UPLOAD_MAX_BYTES:
                    raise HTTPException(413, f"The upload exceeds the {NDI_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB limit.")
            except ValueError as exc:
                raise HTTPException(400, "Invalid Content-Length header.") from exc
        NDI_DROP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with upload_tmp.open("xb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > NDI_UPLOAD_MAX_BYTES:
                    raise HTTPException(413, f"The upload exceeds the {NDI_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB limit.")
                handle.write(chunk)
        os.chmod(upload_tmp, 0o600)
        _validate_ndi_upload_header(upload_tmp, total)
        os.replace(upload_tmp, NDI_DROP_PATH)

        try:
            result = subprocess.run(
                ["sudo", "-n", str(NDI_INSTALL_HELPER)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(500, "NDI runtime verification timed out.") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "NDI runtime verification failed.").strip()
            raise HTTPException(400, detail[-2000:])

        status = _ndi_runtime_status()
        if not status["ready"]:
            raise HTTPException(500, "The runtime installer completed but libndi.so.6 is still unavailable.")
        return {
            "ok": True,
            "message": "The NDI SDK runtime was validated and installed successfully.",
            "runtime": status,
        }
    finally:
        try:
            upload_tmp.unlink(missing_ok=True)
        finally:
            NDI_UPLOAD_LOCK.release()


@app.get("/api/channels")
def api_channels(force_refresh: bool = Query(False)):
    try:
        return {"channels": tvh.list_channels(force_refresh=force_refresh)}
    except Exception as e:
        raise HTTPException(500, f"Failed to list channels: {e}")
@app.get("/api/status")
def api_status(
    lite: bool = Query(False),
    logs: bool = Query(False),
    stats: bool = Query(False),
    rf: bool = Query(True),
):
    """
    Status endpoint.

    - lite=1 returns a small payload suitable for frequent polling.
    - logs=1 / stats=1 can be combined with lite to selectively include heavier fields.
    """
    st = ndi_bridge.status_lite(include_logs=logs, include_stats=stats) if lite else ndi_bridge.status()
    # Backwards-compat for the existing UI: expose active_channel_uuid.
    st["active_channel_uuid"] = st.get("channel_uuid")
    st["active_profile"] = _active_profile
    with NDI_SUPERVISOR_LOCK:
        sup = deepcopy(NDI_SUPERVISOR_STATE)
        req_d = sup.get("request") or {}
    last_req = cfg.get("ndi_last_start_request") if isinstance(cfg.get("ndi_last_start_request"), dict) else None
    st["auto_reconnect_enabled"] = _ndi_supervisor_config()["enabled"]
    if st.get("running"):
        st["active_channel_name"] = req_d.get("channel_name")
        st["active_channel_number"] = req_d.get("channel_number")
    else:
        st["active_channel_name"] = None
        st["active_channel_number"] = None
    if rf:
        st["rf"] = _rf_status_for_channel(
            st.get("channel_uuid") or req_d.get("channel_uuid"),
            st.get("active_channel_name") or req_d.get("channel_name"),
        )
    st["supervisor"] = {
        "desired": bool(sup.get("desired")),
        "restart_count": int(sup.get("restart_count") or 0),
        "last_restart_reason": sup.get("last_restart_reason"),
        "last_start_attempt_at": sup.get("last_start_attempt_at"),
        "last_success_at": sup.get("last_success_at"),
        "last_stop_at": sup.get("last_stop_at"),
        "last_error": sup.get("last_error"),
        "last_stream_url": sup.get("last_stream_url"),
        "pipeline_status": sup.get("pipeline_status"),
        "healthy_since": sup.get("healthy_since"),
        "lineout_desired": bool(sup.get("lineout_desired")),
        "lineout_last_restore_error": sup.get("lineout_last_restore_error"),
        "desired_channel_uuid": req_d.get("channel_uuid"),
        "desired_channel_name": req_d.get("channel_name"),
        "desired_channel_number": req_d.get("channel_number"),
        "desired_profile": req_d.get("profile"),
        "desired_ndi_name": req_d.get("ndi_name"),
        "last_start_request": deepcopy(last_req) if last_req else None,
    }
    return st


@app.get("/api/rf")
def api_rf_status():
    st = ndi_bridge.status_lite(include_logs=False, include_stats=False)
    with NDI_SUPERVISOR_LOCK:
        req_d = deepcopy(NDI_SUPERVISOR_STATE.get("request") or {})
    return _rf_status_for_channel(
        st.get("channel_uuid") or req_d.get("channel_uuid"),
        req_d.get("channel_name"),
    )


UI_CONFIG_KEYS = {
    "tvh_base_url",
    "tvh_dvbt_scanfile",
    "tvh_stream_profile",
    "ndi_default_name",
    "ndi_delay_ms",
    "ndi_deinterlace",
    "ndi_buffer_extra_ms",
    "ndi_qos",
    "ndi_auto_reconnect_enabled",
    "ndi_supervisor_poll_s",
    "ndi_startup_grace_s",
    "ndi_stall_timeout_s",
    "ndi_reconnect_initial_backoff_s",
    "ndi_reconnect_max_backoff_s",
    "ndi_multicast_enabled",
    "ndi_multicast_addr",
    "ndi_multicast_ttl",
    "lineout_default_device",
    "lineout_volume",
    "lineout_sink_sync",
    "lineout_queue_time_ms",
}


class UIConfigUpdateReq(BaseModel):
    tvh_base_url: Optional[str] = None
    tvh_dvbt_scanfile: Optional[str] = None
    tvh_stream_profile: Optional[str] = None
    ndi_default_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    ndi_delay_ms: Optional[int] = Field(default=None, ge=0, le=5000)
    ndi_deinterlace: Optional[bool] = None
    ndi_buffer_extra_ms: Optional[int] = Field(default=None, ge=0, le=5000)
    ndi_qos: Optional[bool] = None
    ndi_auto_reconnect_enabled: Optional[bool] = None
    ndi_supervisor_poll_s: Optional[float] = Field(default=None, ge=0.25, le=30)
    ndi_startup_grace_s: Optional[float] = Field(default=None, ge=1, le=120)
    ndi_stall_timeout_s: Optional[float] = Field(default=None, ge=1, le=120)
    ndi_reconnect_initial_backoff_s: Optional[float] = Field(default=None, ge=0.25, le=300)
    ndi_reconnect_max_backoff_s: Optional[float] = Field(default=None, ge=1, le=600)
    ndi_multicast_enabled: Optional[bool] = None
    ndi_multicast_addr: Optional[str] = None
    ndi_multicast_ttl: Optional[int] = Field(default=None, ge=0, le=255)
    lineout_default_device: Optional[str] = None
    lineout_volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    lineout_sink_sync: Optional[bool] = None
    lineout_queue_time_ms: Optional[int] = Field(default=None, ge=20, le=5000)


@app.get("/api/config/ui")
def api_config_ui():
    live_cfg = deepcopy(cfg)
    return {k: live_cfg.get(k) for k in sorted(UI_CONFIG_KEYS)}


@app.post("/api/config/ui")
def api_config_ui_update(req: UIConfigUpdateReq):
    patch = req.model_dump(exclude_none=True)
    if not patch:
        return {"ok": True, "config": api_config_ui()}
    updated = _update_config(patch)
    return {"ok": True, "config": {k: updated.get(k) for k in sorted(UI_CONFIG_KEYS)}}

class StartReq(BaseModel):
    channel_uuid: str
    ndi_name: str = Field(min_length=1, max_length=80)
    profile: str = Field(default="pass", min_length=1, max_length=40)

    deinterlace: bool = Field(
        default_factory=lambda: bool(cfg.get("ndi_deinterlace", False)),
        description="If true, deinterlace video before sending to NDI (higher CPU).",
    )

    buffer_extra_ms: int = Field(
        default_factory=lambda: int(cfg.get("ndi_buffer_extra_ms", 0)),
        ge=0,
        le=5000,
        description="Extra buffering headroom (ms) for the delayed NDI A/V queues to absorb input jitter.",
    )

    ndi_qos: bool = Field(
        default_factory=lambda: bool(cfg.get("ndi_qos", False)),
        description="If true, enable QoS on ndisink (may drop late frames).",
    )


    ndi_multicast_enabled: bool = Field(
        default_factory=lambda: bool(cfg.get("ndi_multicast_enabled", False)),
        description="Enable NDI multicast for this stream.",
    )

    ndi_multicast_addr: str = Field(
        default_factory=lambda: str(cfg.get("ndi_multicast_addr", "")),
        description="NDI multicast address (required if multicast is enabled).",
    )

    ndi_multicast_ttl: int = Field(
        default_factory=lambda: int(cfg.get("ndi_multicast_ttl", 1)),
        ge=0,
        le=255,
        description="NDI multicast TTL (default 1).",
    )

@app.post("/api/start")
def api_start(req: StartReq):
    global _active_profile
    if _tv_setup_snapshot().get("running"):
        raise HTTPException(409, "TV Setup is running; NDI cannot be started until setup finishes")
    try:
        if req.ndi_multicast_enabled and not str(req.ndi_multicast_addr or "").strip():
            raise HTTPException(400, "Multicast is enabled but no multicast address was provided")
        req_d = _ndi_req_to_dict(req)
        # Mark this as the desired live stream before starting. If GStreamer later
        # receives ERROR/EOS or stops rendering frames, the supervisor will rebuild
        # the pipeline and re-resolve the tvheadend stream URL from this request.
        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE.update({
                "desired": True,
                "request": deepcopy(req_d),
                "was_running": False,
                "restart_count": 0,
                "last_restart_reason": "manual start",
                "last_error": None,
                "last_rendered": None,
                "last_rendered_change_at": time.time(),
                "healthy_since": None,
                "pipeline_status": "starting",
            })
        stream_url = _start_ndi_pipeline_from_dict(req_d, reason="manual start")
    except HTTPException:
        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE["desired"] = False
        raise
    except Exception as e:
        with NDI_SUPERVISOR_LOCK:
            NDI_SUPERVISOR_STATE["desired"] = False
            NDI_SUPERVISOR_STATE["last_error"] = str(e)
        raise HTTPException(500, f"Failed to start pipeline: {e}")
    _active_profile = req.profile
    _update_config({"ndi_default_name": req.ndi_name, "tvh_stream_profile": req.profile, "ndi_last_start_request": req_d})
    return {"ok": True, "stream_url": stream_url, "ndi_name": req.ndi_name, "auto_reconnect": _ndi_supervisor_config()["enabled"]}
@app.post("/api/stop")
def api_stop():
    # Disable the desired stream first so the supervisor does not auto-restart a deliberate stop.
    with NDI_SUPERVISOR_LOCK:
        NDI_SUPERVISOR_STATE.update({
            "desired": False,
            "request": None,
            "last_stop_at": time.time(),
            "was_running": False,
            "last_restart_reason": "manual stop",
            "pipeline_status": "stopped",
            "lineout_desired": False,
            "lineout_request": None,
            "lineout_last_restore_error": None,
        })
    # If NDI stops, line output must stop too.
    try:
        ndi_bridge.lineout_stop()
    except Exception:
        pass
    ndi_bridge.stop()
    return {"ok": True}

def _stop_ndi_for_tv_setup() -> bool:
    """Stop any active or desired NDI/audio pipeline before TV setup mutates Tvheadend."""
    was_running = False
    try:
        st = ndi_bridge.status_lite(include_logs=False, include_stats=False)
        was_running = bool(st.get("running"))
    except Exception:
        was_running = False
    with NDI_SUPERVISOR_LOCK:
        desired_before = bool(NDI_SUPERVISOR_STATE.get("desired"))
        was_running = was_running or desired_before
        NDI_SUPERVISOR_STATE.update({
            "desired": False,
            "request": None,
            "last_stop_at": time.time(),
            "was_running": False,
            "last_restart_reason": "tv setup",
            "pipeline_status": "stopped for tv setup",
            "lineout_desired": False,
            "lineout_request": None,
            "lineout_last_restore_error": None,
        })
    try:
        ndi_bridge.lineout_stop()
    except Exception:
        pass
    try:
        ndi_bridge.stop()
    except Exception:
        pass
    return was_running

@app.get("/api/tv/setup/status")
def api_tv_setup_status():
    return _tv_setup_snapshot()

@app.get("/api/tv/setup/regions")
def api_tv_setup_regions():
    try:
        regions = tvh.list_dvb_scanfiles("dvb-t")
        selected = _preferred_dvbt_scanfile(regions, str(cfg.get("tvh_dvbt_scanfile") or ""))
        return {"regions": regions, "selected": selected}
    except Exception as e:
        raise HTTPException(500, f"Failed to load TV predefined mux regions: {e}")

class TVSetupRunReq(BaseModel):
    scanfile: Optional[str] = None

@app.post("/api/tv/setup/run")
def api_tv_setup_run(req: TVSetupRunReq):
    snap = _tv_setup_snapshot()
    if snap.get("running"):
        raise HTTPException(409, "TV Setup is already running")
    scanfile_key = str(req.scanfile or "").strip() or None
    if not scanfile_key:
        try:
            regions = tvh.list_dvb_scanfiles("dvb-t")
            scanfile_key = _preferred_dvbt_scanfile(regions, str(cfg.get("tvh_dvbt_scanfile") or "")) or None
        except Exception as e:
            raise HTTPException(500, f"Could not choose default TV DVB-T/T2 predefined mux region: {e}")
    if scanfile_key:
        try:
            valid = {str(r.get("key") or "") for r in tvh.list_dvb_scanfiles("dvb-t")}
        except Exception as e:
            raise HTTPException(500, f"Could not validate selected region with TV: {e}")
        if scanfile_key not in valid:
            raise HTTPException(400, f"Unknown TV DVB-T/T2 predefined mux region: {scanfile_key}")
    _tv_setup_set(
        running=True,
        done=False,
        partial=False,
        percent=1,
        step="Starting TV Setup…",
        error=None,
        logs=[],
        started_at=int(time.time()),
        finished_at=None,
        selected_scanfile=scanfile_key,
        scan_note=None,
    )
    t = threading.Thread(target=_run_tv_setup_worker, args=(scanfile_key,), name="tv-setup-worker", daemon=True)
    t.start()
    return {"ok": True, "scanfile": scanfile_key}
# ---------------- Line output ----------------
class LineOutStartReq(BaseModel):
    device_id: Optional[str] = Field(default_factory=lambda: cfg.get("lineout_default_device"))
    volume: float = Field(default_factory=lambda: float(cfg.get("lineout_volume", 0.8)), ge=0.0, le=1.0)


@app.get("/api/audio/status")
def api_audio_status(logs: bool = Query(True)):
    return ndi_bridge.lineout_status(include_logs=logs)


@app.get("/api/audio/devices")
def api_audio_devices():
    devices = ndi_bridge.audio_output_devices()
    selected = str(cfg.get("lineout_default_device") or "")
    if not selected and devices:
        selected = str(devices[0].get("id") or "")
    return {
        "devices": devices,
        "selected": selected,
        "volume": float(cfg.get("lineout_volume", 0.8)),
    }


@app.get("/api/audio/defaults")
def api_audio_defaults():
    devices = ndi_bridge.audio_output_devices()
    selected = str(cfg.get("lineout_default_device") or "")
    if not selected and devices:
        selected = str(devices[0].get("id") or "")
    return {
        "device_id": selected,
        "volume": float(cfg.get("lineout_volume", 0.8)),
        "sink_sync": bool(cfg.get("lineout_sink_sync", True)),
    }


@app.post("/api/audio/start")
def api_audio_start(req: LineOutStartReq):
    if TV_SETUP_STATE.get("running"):
        raise HTTPException(409, "TV Setup is running; audio output cannot be started until setup finishes")
    ndi_st = ndi_bridge.status_lite()
    if not ndi_st.get("running"):
        raise HTTPException(400, "NDI stream must be running before audio output can be started.")

    try:
        ndi_bridge.lineout_start(device_id=req.device_id, volume=req.volume)
    except Exception as e:
        raise HTTPException(500, f"Failed to start audio output: {e}")
    with NDI_SUPERVISOR_LOCK:
        NDI_SUPERVISOR_STATE["lineout_desired"] = True
        NDI_SUPERVISOR_STATE["lineout_request"] = _lineout_req_to_dict(req)
        NDI_SUPERVISOR_STATE["lineout_last_restore_error"] = None
    _update_config({
        "lineout_default_device": req.device_id,
        "lineout_volume": req.volume,
    })
    st = ndi_bridge.lineout_status()
    return {"ok": True, "status": st}


@app.post("/api/audio/stop")
def api_audio_stop():
    with NDI_SUPERVISOR_LOCK:
        NDI_SUPERVISOR_STATE["lineout_desired"] = False
        NDI_SUPERVISOR_STATE["lineout_request"] = None
        NDI_SUPERVISOR_STATE["lineout_last_restore_error"] = None
    ndi_bridge.lineout_stop()
    return {"ok": True}
fleet_manager.configure(
    get_config=lambda: cfg,
    update_config=_update_stored_config,
    get_local_status=api_status,
    get_release_info=system_manager.release_info,
    get_hostname=system_manager.persistent_hostname,
)
app.include_router(fleet_manager.router)
app.include_router(system_manager.router)
