import json
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import requests
from tvh import TvheadendClient
from gst_ndi import GstNDIBridge
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_LOCK = threading.Lock()


def _load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError("Missing config.json (create it and set tvh_base_url).")
    return json.loads(CONFIG_PATH.read_text())


def _save_config(next_cfg: Dict[str, Any]) -> Dict[str, Any]:
    CONFIG_PATH.write_text(json.dumps(next_cfg, indent=2) + "\n")
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


cfg = _load_config()

tvh = _build_tvh_client(cfg)
ndi_bridge = GstNDIBridge(config=cfg)

# TVH stream profile used to resolve the current channel's stream URL.
_active_profile: str = cfg.get("tvh_stream_profile", "pass")

# Default (fixed) NDI delay applied when starting the pipeline
NDI_DELAY_DEFAULT_MS: int = int(cfg.get("ndi_delay_ms", 250))


# ---------------- NDI supervision / auto-reconnect ----------------
# The live tvheadend stream is consumed by GStreamer, not by TvheadendClient.
# If tvheadend drops/stalls the HTTP stream, GStreamer can post ERROR/EOS or
# simply stop rendering frames. This supervisor owns the desired channel state
# and restarts the NDI pipeline with a freshly resolved tvheadend URL.
NDI_SUPERVISOR_LOCK = threading.RLock()
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
        "stall_timeout_s": max(1.0, float(cfg.get("ndi_stall_timeout_s", 5.0))),
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
    while True:
        cfg_s = _ndi_supervisor_config()
        time.sleep(cfg_s["poll_s"])
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
                    if prev is None or rendered_i != int(prev):
                        NDI_SUPERVISOR_STATE["last_rendered"] = rendered_i
                        NDI_SUPERVISOR_STATE["last_rendered_change_at"] = now
                        last_change = now
                        if NDI_SUPERVISOR_STATE.get("healthy_since") is None:
                            NDI_SUPERVISOR_STATE["healthy_since"] = now
                        if NDI_SUPERVISOR_STATE.get("last_success_at") is None:
                            NDI_SUPERVISOR_STATE["last_success_at"] = now
                    if (now - float(started_at)) >= cfg_s["startup_grace_s"] and (now - float(last_change)) >= cfg_s["stall_timeout_s"]:
                        should_restart = True
                        restart_reason = f"stall: no NDI frames rendered for {cfg_s['stall_timeout_s']:.1f}s"
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


threading.Thread(target=_ndi_supervisor_loop, name="ndi-supervisor", daemon=True).start()

TV_SETUP_STATE: Dict[str, Any] = {
    "running": False,
    "done": False,
    "percent": 0,
    "step": "Idle",
    "logs": [],
    "error": None,
    "started_at": None,
    "finished_at": None,
    "selected_scanfile": None,
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


def _wait_for_scan(network_uuid: str, timeout_s: int = 600) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout_s
    stable = 0
    last_summary = None
    diag_every = 0
    while time.time() < deadline:
        muxes = tvh.list_muxes_for_network(network_uuid)
        active = 0
        total_services = 0
        complete = 0
        for mux in muxes:
            scan_state = int(mux.get("scan_state") or 0)
            if scan_state != 0:
                active += 1
            else:
                complete += 1
            total_services += int(mux.get("num_svc") or 0)
        summary = (len(muxes), active, complete, total_services)
        if summary != last_summary:
            _tv_setup_log(f"Scan progress: muxes={len(muxes)} active={active} complete={complete} services={total_services}")
            last_summary = summary
            diag_every += 1
            if diag_every >= 3 or (muxes and active == 0):
                _log_mux_diagnostics(muxes, prefix="Mux status")
                diag_every = 0
        if muxes and active == 0:
            stable += 1
            if stable >= 3:
                return muxes
        else:
            stable = 0
        time.sleep(3)
    raise RuntimeError("Timed out waiting for DVB-T scan to finish")

def _wait_for_mapper(timeout_s: int = 300) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last_summary = None
    while time.time() < deadline:
        status = tvh.mapper_status()
        total = int(status.get("total") or 0)
        done = int(status.get("ok") or 0) + int(status.get("fail") or 0) + int(status.get("ignore") or 0)
        summary = (total, done, int(status.get("ok") or 0), int(status.get("ignore") or 0), int(status.get("fail") or 0))
        if summary != last_summary:
            _tv_setup_log(f"Mapper progress: {done}/{total} processed (ok={summary[2]}, ignore={summary[3]}, fail={summary[4]})")
            last_summary = summary
        if total == 0 or done >= total:
            return status
        time.sleep(2)
    raise RuntimeError("Timed out waiting for TVHeadend service mapper")

def _run_tv_setup_worker(scanfile_key: Optional[str] = None) -> None:
    try:
        scanfile_key = str(scanfile_key or cfg.get("tvh_dvbt_scanfile") or "").strip() or None
        _tv_setup_set(running=True, done=False, percent=2, step="Stopping NDI before TV Setup…", error=None, selected_scanfile=scanfile_key)
        if _stop_ndi_for_tv_setup():
            _tv_setup_log("Stopped active NDI/audio pipeline before TV Setup.")
        else:
            _tv_setup_log("Confirmed NDI pipeline is stopped before TV Setup.")
        _tv_setup_set(percent=4, step="Loading current TVHeadend data…")
        if scanfile_key:
            _tv_setup_log(f"Selected predefined DVB-T/T2 mux region: {scanfile_key}")
        channels = tvh.list_channels(force_refresh=True)
        _tv_setup_log(f"Found {len(channels)} existing channel(s).")

        _tv_setup_set(percent=14, step="Deleting channels…")
        tvh.delete_channels([c["uuid"] for c in channels if c.get("uuid")])
        time.sleep(1)
        channels_after = tvh.list_channels(force_refresh=True)
        _tv_setup_log(f"Channels remaining after delete: {len(channels_after)}")

        _tv_setup_set(percent=28, step="Loading existing services…")
        services = tvh.list_services(hidemode="none")
        _tv_setup_log(f"Found {len(services)} existing service(s).")

        _tv_setup_set(percent=40, step="Deleting services…")
        tvh.delete_services([s["uuid"] for s in services if s.get("uuid")])
        time.sleep(1)
        services_after = tvh.list_services(hidemode="none")
        _tv_setup_log(f"Services remaining after delete: {len(services_after)}")

        _tv_setup_set(percent=50, step="Resolving DVB-T network…")
        network = _resolve_dvbt_network()
        network_uuid = str(network.get("uuid") or "")
        network_name = str(network.get("name") or network_uuid)
        if not network_uuid:
            raise RuntimeError("Resolved DVB-T network is missing a uuid")
        _tv_setup_log(f"Using DVB-T network: {network_name} ({network_uuid})")

        if scanfile_key:
            _tv_setup_set(percent=55, step="Applying selected predefined muxes…")
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

        _tv_setup_set(percent=58, step="Starting DVB-T scan…")
        tvh.scan_network(network_uuid)
        _tv_setup_log("Requested DVB-T network scan.")

        _tv_setup_set(percent=68, step="Scanning muxes and discovering services…")
        muxes = _wait_for_scan(network_uuid)
        _tv_setup_log(f"Scan finished across {len(muxes)} mux(es).")

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
            raise RuntimeError("No services were discovered after the DVB-T scan")

        _tv_setup_set(percent=90, step="Mapping services to channels…")
        tvh.map_services(service_uuids)
        _wait_for_mapper()

        _tv_setup_set(percent=97, step="Refreshing channel list…")
        mapped_channels = tvh.list_channels(force_refresh=True)
        _tv_setup_log(f"Mapped {len(mapped_channels)} channel(s).")

        _tv_setup_set(running=False, done=True, percent=100, step="TV Setup complete", finished_at=int(time.time()))
    except Exception as e:
        _tv_setup_log(f"ERROR: {e}")
        _tv_setup_set(running=False, done=True, percent=100, step="TV Setup failed", error=str(e), finished_at=int(time.time()))

app = FastAPI(title="Tvheadend to NDI/Line Audio Bridge")
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
# ---------------- Static pages ----------------
def _ensure_static_pages():
    """
    This app historically serves pages from ./static.
    To avoid surprises (and to make local dev easier), we also copy any root-level
    *.html into ./static if the static copy is missing.
    """
    for name in ("index.html", "audio.html", "system.html", "manager.html", "common.css", "common.js"):
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
def manager_page(request: Request):
    adoption = _manager_adoption_snapshot()
    manager_url = str(adoption.get("manager_url") or "").strip()
    current_url = str(request.url).rstrip("/")
    if adoption.get("adopted") and manager_url and manager_url.rstrip("/") != current_url:
        return RedirectResponse(manager_url, status_code=302)
    page = static_dir / "manager.html"
    if not page.exists():
        raise HTTPException(500, "static/manager.html missing")
    return FileResponse(str(page))
# ---------------- Existing API ----------------
@app.get("/api/channels")
def api_channels(force_refresh: bool = Query(False)):
    try:
        return {"channels": tvh.list_channels(force_refresh=force_refresh)}
    except Exception as e:
        raise HTTPException(500, f"Failed to list channels: {e}")
@app.get("/api/status")
def api_status(lite: bool = Query(False), logs: bool = Query(False), stats: bool = Query(False)):
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
    st["auto_reconnect_enabled"] = _ndi_supervisor_config()["enabled"]
    if st.get("running"):
        st["active_channel_name"] = req_d.get("channel_name")
        st["active_channel_number"] = req_d.get("channel_number")
    else:
        st["active_channel_name"] = None
        st["active_channel_number"] = None
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
    }
    return st


MANAGER_CONFIG_KEY = "manager_units"
MANAGER_ID_CONFIG_KEY = "manager_id"
MANAGER_CONNECT_TIMEOUT_S = 0.7
MANAGER_READ_TIMEOUT_S = 1.8
MANAGER_ADOPTION_TTL_S = 20.0
MANAGER_ADOPTION_LOCK = threading.Lock()
MANAGER_ADOPTION_STATE: Dict[str, Any] = {
    "manager_id": None,
    "manager_url": None,
    "manager_name": None,
    "last_seen_at": None,
    "expires_at": None,
}


def _manager_identity(manager_url: Optional[str] = None) -> Dict[str, str]:
    manager_id = str(cfg.get(MANAGER_ID_CONFIG_KEY) or "").strip()
    if not manager_id:
        manager_id = uuid.uuid4().hex
        _update_stored_config({MANAGER_ID_CONFIG_KEY: manager_id})
    return {
        "manager_id": manager_id,
        "manager_url": str(manager_url or "").strip(),
        "manager_name": socket.gethostname() or "TeleTool Manager",
    }


def _manager_adoption_snapshot() -> Dict[str, Any]:
    now = time.time()
    with MANAGER_ADOPTION_LOCK:
        expires_at = float(MANAGER_ADOPTION_STATE.get("expires_at") or 0)
        adopted = bool(MANAGER_ADOPTION_STATE.get("manager_id")) and expires_at > now
        if not adopted:
            MANAGER_ADOPTION_STATE.update({
                "manager_id": None,
                "manager_url": None,
                "manager_name": None,
                "last_seen_at": None,
                "expires_at": None,
            })
        return {
            "adopted": adopted,
            "manager_id": MANAGER_ADOPTION_STATE.get("manager_id") if adopted else None,
            "manager_url": MANAGER_ADOPTION_STATE.get("manager_url") if adopted else None,
            "manager_name": MANAGER_ADOPTION_STATE.get("manager_name") if adopted else None,
            "last_seen_at": MANAGER_ADOPTION_STATE.get("last_seen_at") if adopted else None,
            "expires_at": MANAGER_ADOPTION_STATE.get("expires_at") if adopted else None,
            "ttl_s": MANAGER_ADOPTION_TTL_S,
        }


def _manager_adoption_heartbeat(manager_id: str, manager_url: Optional[str], manager_name: Optional[str]) -> Tuple[bool, Dict[str, Any]]:
    now = time.time()
    manager_id = str(manager_id or "").strip()
    if not manager_id:
        raise ValueError("manager_id is required")
    with MANAGER_ADOPTION_LOCK:
        expires_at = float(MANAGER_ADOPTION_STATE.get("expires_at") or 0)
        existing_id = str(MANAGER_ADOPTION_STATE.get("manager_id") or "")
        if existing_id and existing_id != manager_id and expires_at > now:
            return False, {
                "adopted": True,
                "manager_id": existing_id,
                "manager_url": MANAGER_ADOPTION_STATE.get("manager_url"),
                "manager_name": MANAGER_ADOPTION_STATE.get("manager_name"),
                "last_seen_at": MANAGER_ADOPTION_STATE.get("last_seen_at"),
                "expires_at": MANAGER_ADOPTION_STATE.get("expires_at"),
                "ttl_s": MANAGER_ADOPTION_TTL_S,
            }
        MANAGER_ADOPTION_STATE.update({
            "manager_id": manager_id,
            "manager_url": str(manager_url or "").strip() or None,
            "manager_name": str(manager_name or "").strip() or None,
            "last_seen_at": now,
            "expires_at": now + MANAGER_ADOPTION_TTL_S,
        })
        return True, {
            "adopted": True,
            "manager_id": manager_id,
            "manager_url": MANAGER_ADOPTION_STATE.get("manager_url"),
            "manager_name": MANAGER_ADOPTION_STATE.get("manager_name"),
            "last_seen_at": MANAGER_ADOPTION_STATE.get("last_seen_at"),
            "expires_at": MANAGER_ADOPTION_STATE.get("expires_at"),
            "ttl_s": MANAGER_ADOPTION_TTL_S,
        }


def _manager_adoption_release(manager_id: str) -> Dict[str, Any]:
    now = time.time()
    manager_id = str(manager_id or "").strip()
    with MANAGER_ADOPTION_LOCK:
        existing_id = str(MANAGER_ADOPTION_STATE.get("manager_id") or "")
        expires_at = float(MANAGER_ADOPTION_STATE.get("expires_at") or 0)
        if not existing_id or expires_at <= now or existing_id == manager_id:
            MANAGER_ADOPTION_STATE.update({
                "manager_id": None,
                "manager_url": None,
                "manager_name": None,
                "last_seen_at": None,
                "expires_at": None,
            })
    return _manager_adoption_snapshot()


def _validate_manager_hostname(hostname: str) -> None:
    value = str(hostname or "").strip()
    if not value:
        raise ValueError("Host is required")
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    if len(value) > 253:
        raise ValueError("Hostname is too long")
    labels = value.rstrip(".").split(".")
    hostname_label_re = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
    if not labels or any(not hostname_label_re.match(label) for label in labels):
        raise ValueError("Enter a valid IP address or hostname")


def _normalise_manager_target(raw_host: str) -> Dict[str, Any]:
    raw = str(raw_host or "").strip()
    if not raw:
        raise ValueError("IP address or hostname is required")

    candidate = raw if re.match(r"^https?://", raw, flags=re.IGNORECASE) else f"http://{raw}"
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs are supported")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Enter a valid IP address or hostname")
    _validate_manager_hostname(hostname)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Port must be between 1 and 65535")
    port = int(port or 8000)
    if port < 1 or port > 65535:
        raise ValueError("Port must be between 1 and 65535")

    url_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    return {
        "host": hostname,
        "port": port,
        "scheme": scheme,
        "address": f"{url_host}:{port}",
        "base_url": f"{scheme}://{url_host}:{port}",
    }


def _manager_units_from_config() -> List[Dict[str, Any]]:
    raw_units = cfg.get(MANAGER_CONFIG_KEY, [])
    if not isinstance(raw_units, list):
        return []
    units: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_units:
        if not isinstance(item, dict):
            continue
        target = item.get("base_url") or item.get("address") or item.get("host")
        try:
            normalised = _normalise_manager_target(str(target or ""))
        except ValueError:
            continue
        key = normalised["base_url"].lower()
        if key in seen:
            continue
        seen.add(key)
        unit_id = str(item.get("id") or "").strip()
        if not unit_id:
            unit_id = "unit-" + re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")
        units.append({
            "id": unit_id,
            "host": normalised["host"],
            "address": normalised["address"],
            "base_url": normalised["base_url"],
            "scheme": normalised["scheme"],
            "port": normalised["port"],
        })
    return units


def _manager_channel_label(status: Dict[str, Any], base_url: str) -> Tuple[Optional[str], Optional[Any], Optional[str]]:
    channel_uuid = status.get("channel_uuid") or status.get("active_channel_uuid")
    channel_name = status.get("active_channel_name")
    channel_number = status.get("active_channel_number")
    if channel_uuid and not channel_name:
        try:
            data = _manager_fetch_json(base_url, "/api/channels")
            for channel in data.get("channels", []):
                if channel.get("uuid") == channel_uuid:
                    channel_name = channel.get("name")
                    channel_number = channel.get("number")
                    break
        except Exception:
            pass

    if channel_name:
        if channel_number not in (None, ""):
            return str(channel_name), channel_number, f"{channel_number} {channel_name}"
        return str(channel_name), channel_number, str(channel_name)
    if channel_uuid:
        return None, None, str(channel_uuid)
    return None, None, None


def _manager_fetch_json(base_url: str, path: str) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path
    response = requests.get(
        url,
        timeout=(MANAGER_CONNECT_TIMEOUT_S, MANAGER_READ_TIMEOUT_S),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as e:
        raise RuntimeError(f"{path} returned non-JSON: {e}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} returned an unexpected payload")
    return data


def _manager_post_json(base_url: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path
    response = requests.post(
        url,
        json=payload,
        timeout=(MANAGER_CONNECT_TIMEOUT_S, MANAGER_READ_TIMEOUT_S),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as e:
        raise RuntimeError(f"{path} returned non-JSON: {e}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} returned an unexpected payload")
    return data


def _manager_heartbeat_unit(unit: Dict[str, Any], manager_identity: Dict[str, str]) -> Dict[str, Any]:
    try:
        data = _manager_post_json(unit["base_url"], "/api/manager/adoption/heartbeat", manager_identity)
        return {"ok": True, "adoption": data.get("adoption") or {}}
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 409:
            return {"ok": False, "error": "Adopted by another active manager"}
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _manager_release_unit(unit: Dict[str, Any], manager_identity: Dict[str, str]) -> None:
    try:
        _manager_post_json(
            unit["base_url"],
            "/api/manager/adoption/release",
            {"manager_id": manager_identity.get("manager_id", "")},
        )
    except Exception:
        pass


def _manager_status_for_unit(unit: Dict[str, Any], manager_identity: Dict[str, str]) -> Dict[str, Any]:
    checked_at = int(time.time())
    started = time.monotonic()
    result: Dict[str, Any] = {
        **unit,
        "main_url": unit["base_url"].rstrip("/") + "/",
        "online": False,
        "system_status": "offline",
        "stream_running": False,
        "stream_status": "unknown",
        "pipeline_state": None,
        "pipeline_status": None,
        "hostname": None,
        "ndi_name": None,
        "default_ndi_name": None,
        "channel_uuid": None,
        "channel_name": None,
        "channel_number": None,
        "channel_label": None,
        "started_at": None,
        "last_error": None,
        "last_warning": None,
        "adoption_ok": False,
        "adoption_error": None,
        "error": None,
        "checked_at": checked_at,
        "latency_ms": None,
    }

    try:
        status = _manager_fetch_json(unit["base_url"], "/api/status?lite=1")
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result

    result["online"] = True
    result["system_status"] = "online"
    adoption = _manager_heartbeat_unit(unit, manager_identity)
    result["adoption_ok"] = bool(adoption.get("ok"))
    result["adoption_error"] = adoption.get("error")
    supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
    running = bool(status.get("running"))
    result.update({
        "stream_running": running,
        "stream_status": "running" if running else "stopped",
        "pipeline_state": status.get("pipeline_state"),
        "pipeline_status": supervisor.get("pipeline_status"),
        "channel_uuid": status.get("channel_uuid") or status.get("active_channel_uuid"),
        "started_at": status.get("started_at"),
        "last_error": status.get("last_error") or supervisor.get("last_error"),
        "last_warning": status.get("last_warning"),
    })

    try:
        host_info = _manager_fetch_json(unit["base_url"], "/api/system/hostname")
        hostname = host_info.get("hostname")
        if isinstance(hostname, str) and hostname.strip():
            result["hostname"] = hostname.strip()
    except Exception:
        pass

    try:
        unit_config = _manager_fetch_json(unit["base_url"], "/api/config/ui")
        default_ndi_name = unit_config.get("ndi_default_name")
        if isinstance(default_ndi_name, str) and default_ndi_name.strip():
            result["default_ndi_name"] = default_ndi_name.strip()
    except Exception:
        pass

    ndi_name = status.get("ndi_name") or supervisor.get("desired_ndi_name") or result["default_ndi_name"]
    if isinstance(ndi_name, str) and ndi_name.strip():
        result["ndi_name"] = ndi_name.strip()

    if running:
        channel_name, channel_number, channel_label = _manager_channel_label(status, unit["base_url"])
        result["channel_name"] = channel_name
        result["channel_number"] = channel_number
        result["channel_label"] = channel_label

    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _manager_status_for_self(base_url: str) -> Dict[str, Any]:
    checked_at = int(time.time())
    parsed = urlparse(base_url)
    hostname = socket.gethostname() or "TeleTool"
    status = api_status(lite=True, logs=False, stats=False)
    supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
    running = bool(status.get("running"))
    channel_name = status.get("active_channel_name")
    channel_number = status.get("active_channel_number")
    channel_uuid = status.get("channel_uuid") or status.get("active_channel_uuid")
    channel_label = None
    if running:
        if channel_name:
            channel_label = f"{channel_number} {channel_name}" if channel_number not in (None, "") else str(channel_name)
        elif channel_uuid:
            channel_label = str(channel_uuid)

    default_ndi_name = str(cfg.get("ndi_default_name") or "").strip() or None
    ndi_name = status.get("ndi_name") or supervisor.get("desired_ndi_name") or default_ndi_name

    return {
        "id": "__self__",
        "is_self": True,
        "removable": False,
        "role": "primary",
        "host": parsed.hostname or hostname,
        "address": parsed.netloc or parsed.hostname or hostname,
        "base_url": base_url,
        "main_url": base_url.rstrip("/") + "/",
        "scheme": parsed.scheme or "http",
        "port": parsed.port,
        "online": True,
        "system_status": "online",
        "stream_running": running,
        "stream_status": "running" if running else "stopped",
        "pipeline_state": status.get("pipeline_state"),
        "pipeline_status": supervisor.get("pipeline_status"),
        "hostname": hostname,
        "ndi_name": str(ndi_name).strip() if ndi_name else None,
        "default_ndi_name": default_ndi_name,
        "channel_uuid": channel_uuid,
        "channel_name": channel_name,
        "channel_number": channel_number,
        "channel_label": channel_label,
        "started_at": status.get("started_at"),
        "last_error": status.get("last_error") or supervisor.get("last_error"),
        "last_warning": status.get("last_warning"),
        "adoption_ok": True,
        "adoption_error": None,
        "error": None,
        "checked_at": checked_at,
        "latency_ms": 0,
    }


class ManagerAdoptionHeartbeatReq(BaseModel):
    manager_id: str = Field(min_length=1, max_length=120)
    manager_url: Optional[str] = Field(default=None, max_length=500)
    manager_name: Optional[str] = Field(default=None, max_length=120)


class ManagerAdoptionReleaseReq(BaseModel):
    manager_id: str = Field(min_length=1, max_length=120)


@app.get("/api/manager/units")
def api_manager_units():
    return {"units": _manager_units_from_config()}


@app.get("/api/manager/adoption")
def api_manager_adoption():
    return _manager_adoption_snapshot()


@app.post("/api/manager/adoption/heartbeat")
def api_manager_adoption_heartbeat(req: ManagerAdoptionHeartbeatReq):
    try:
        ok, adoption = _manager_adoption_heartbeat(req.manager_id, req.manager_url, req.manager_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        manager_name = adoption.get("manager_name") or adoption.get("manager_url") or "another active manager"
        raise HTTPException(409, f"Already adopted by {manager_name}")
    return {"ok": True, "adoption": adoption}


@app.post("/api/manager/adoption/release")
def api_manager_adoption_release(req: ManagerAdoptionReleaseReq):
    return {"ok": True, "adoption": _manager_adoption_release(req.manager_id)}


class ManagerUnitReq(BaseModel):
    host: str = Field(min_length=1, max_length=300)


@app.post("/api/manager/units")
def api_manager_add_unit(req: ManagerUnitReq):
    try:
        target = _normalise_manager_target(req.host)
    except ValueError as e:
        raise HTTPException(400, str(e))
    units = _manager_units_from_config()
    if any(unit["base_url"].lower() == target["base_url"].lower() for unit in units):
        raise HTTPException(409, "That TeleTool unit is already listed")
    new_unit = {
        "id": uuid.uuid4().hex,
        "host": target["host"],
        "address": target["address"],
        "base_url": target["base_url"],
        "scheme": target["scheme"],
        "port": target["port"],
    }
    _update_stored_config({MANAGER_CONFIG_KEY: units + [new_unit]})
    return {"ok": True, "unit": new_unit, "units": _manager_units_from_config()}


@app.delete("/api/manager/units/{unit_id}")
def api_manager_delete_unit(unit_id: str):
    units = _manager_units_from_config()
    removed_units = [unit for unit in units if unit["id"] == unit_id]
    next_units = [unit for unit in units if unit["id"] != unit_id]
    if len(next_units) == len(units):
        raise HTTPException(404, "TeleTool unit not found")
    manager_identity = _manager_identity()
    for unit in removed_units:
        _manager_release_unit(unit, manager_identity)
    _update_stored_config({MANAGER_CONFIG_KEY: next_units})
    return {"ok": True, "units": _manager_units_from_config()}


@app.get("/api/manager/status")
def api_manager_status(request: Request):
    base_url = str(request.base_url).rstrip("/")
    self_status = _manager_status_for_self(base_url)
    units = _manager_units_from_config()
    if not units:
        return {"units": [self_status], "checked_at": int(time.time())}

    manager_identity = _manager_identity(base_url + "/manager")
    statuses: List[Optional[Dict[str, Any]]] = [None] * len(units)
    max_workers = min(12, max(1, len(units)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(_manager_status_for_unit, unit, manager_identity): index for index, unit in enumerate(units)}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                statuses[index] = future.result()
            except Exception as e:
                failed = dict(units[index])
                failed.update({
                    "main_url": failed["base_url"].rstrip("/") + "/",
                    "online": False,
                    "system_status": "offline",
                    "stream_running": False,
                    "stream_status": "unknown",
                    "error": str(e),
                    "checked_at": int(time.time()),
                })
                statuses[index] = failed

    return {"units": [self_status] + [status for status in statuses if status is not None], "checked_at": int(time.time())}


UI_CONFIG_KEYS = {
    "tvh_base_url",
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
    _update_config({"ndi_default_name": req.ndi_name, "tvh_stream_profile": req.profile})
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
        selected = str(cfg.get("tvh_dvbt_scanfile") or "")
        return {"regions": regions, "selected": selected}
    except Exception as e:
        raise HTTPException(500, f"Failed to load Tvheadend predefined mux regions: {e}")

class TVSetupRunReq(BaseModel):
    scanfile: Optional[str] = None

@app.post("/api/tv/setup/run")
def api_tv_setup_run(req: TVSetupRunReq):
    snap = _tv_setup_snapshot()
    if snap.get("running"):
        raise HTTPException(409, "TV Setup is already running")
    scanfile_key = str(req.scanfile or "").strip() or None
    if scanfile_key:
        try:
            valid = {str(r.get("key") or "") for r in tvh.list_dvb_scanfiles("dvb-t")}
        except Exception as e:
            raise HTTPException(500, f"Could not validate selected region with Tvheadend: {e}")
        if scanfile_key not in valid:
            raise HTTPException(400, f"Unknown Tvheadend DVB-T/T2 predefined mux region: {scanfile_key}")
    _tv_setup_set(
        running=True,
        done=False,
        percent=1,
        step="Starting TV Setup…",
        error=None,
        logs=[],
        started_at=int(time.time()),
        finished_at=None,
        selected_scanfile=scanfile_key,
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
# ---------------- System helpers + API ----------------
NETWORK_PRIVILEGE_HELP = (
    "Network changes need root privileges. The web service tried to run the required "
    "network command directly and with sudo -n, but the operating system did not allow it. "
    "Run install_network_privileges.sh once, or configure passwordless sudo for the "
    "tvh_ndi_bridge service user and nmcli/systemctl network commands."
)

GITHUB_UPDATE_ZIP_URL = "https://github.com/JohnDevAc/teletwat/archive/refs/heads/main.zip"
UPDATE_EXCLUDED_NAMES = {
    ".git",
    ".venv",
    "venv",
    "golden-images",
    "__pycache__",
    ".pytest_cache",
    "config.json",
    ".env",
    ".env.local",
}
UPDATE_LOCK = threading.Lock()
UPDATE_STATE: Dict[str, Any] = {
    "running": False,
    "done": False,
    "percent": 0,
    "step": "Idle",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "stats": None,
}


def _set_update_status(**patch: Any) -> Dict[str, Any]:
    with UPDATE_LOCK:
        UPDATE_STATE.update(patch)
        return deepcopy(UPDATE_STATE)


def _update_status_snapshot() -> Dict[str, Any]:
    with UPDATE_LOCK:
        return deepcopy(UPDATE_STATE)


def _schedule_program_restart(delay_s: float = 0.5, exit_code: int = 3) -> None:
    def _do_exit():
        time.sleep(delay_s)
        os._exit(exit_code)
    threading.Thread(target=_do_exit, daemon=True).start()


def _is_update_excluded(rel_path: Path) -> bool:
    parts = rel_path.parts
    if not parts:
        return True
    if any(part in UPDATE_EXCLUDED_NAMES for part in parts):
        return True
    name = rel_path.name
    if name.endswith(".pyc"):
        return True
    if name.startswith(".env."):
        return True
    return False


def _download_github_update_archive(dest: Path) -> int:
    req = UrlRequest(
        GITHUB_UPDATE_ZIP_URL,
        headers={
            "User-Agent": "TeleTool updater",
            "Accept": "application/zip,application/octet-stream,*/*",
        },
    )
    with urlopen(req, timeout=30) as response:
        total = 0
        with dest.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                out.write(chunk)
    return total


def _extract_github_archive(archive_path: Path, dest_dir: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        names = [n for n in archive.namelist() if n and not n.endswith("/")]
        if not names:
            raise RuntimeError("Downloaded update package was empty")
        root = names[0].split("/", 1)[0]
        source_dir = dest_dir / root
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            parts = Path(name).parts
            if not parts or parts[0] != root:
                continue
            rel = Path(*parts[1:])
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"Unsafe path in update package: {name}")
            target = dest_dir / root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
        if not source_dir.exists():
            raise RuntimeError("Downloaded update package did not contain a project folder")
        return source_dir


def _copy_update_files(source_dir: Path, project_dir: Path) -> Dict[str, Any]:
    copied = 0
    skipped = 0
    for src in source_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source_dir)
        if _is_update_excluded(rel):
            skipped += 1
            continue
        dst = project_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    for executable in (
        project_dir / "scripts" / "pi_setup.sh",
        project_dir / "scripts" / "pi_make_golden_image.sh",
        project_dir / "install_network_privileges.sh",
    ):
        if executable.exists():
            try:
                executable.chmod(executable.stat().st_mode | 0o111)
            except Exception:
                pass

    return {"copied": copied, "skipped": skipped}


def _run_program_update_worker() -> None:
    try:
        _set_update_status(percent=8, step="Preparing update", error=None)
        with tempfile.TemporaryDirectory(prefix="teletool-update-") as tmp_s:
            tmp = Path(tmp_s)
            archive = tmp / "update.zip"

            _set_update_status(percent=20, step="Downloading update")
            bytes_downloaded = _download_github_update_archive(archive)

            _set_update_status(percent=50, step="Preparing files")
            source_dir = _extract_github_archive(archive, tmp / "src")

            _set_update_status(percent=75, step="Installing update")
            copy_stats = _copy_update_files(source_dir, BASE_DIR)
            copy_stats["bytes_downloaded"] = bytes_downloaded

        _set_update_status(
            running=False,
            done=True,
            percent=100,
            step="Update complete. Restarting program.",
            error=None,
            finished_at=int(time.time()),
            stats=copy_stats,
        )
        _schedule_program_restart(1.0)
    except Exception as e:
        _set_update_status(
            running=False,
            done=True,
            percent=100,
            step="Update failed",
            error=str(e),
            finished_at=int(time.time()),
        )

def _run_cmd(argv: List[str], sudo: bool = False, timeout_s: int = 8) -> Tuple[int, str, str]:
    """
    Run a command safely.
    - If sudo=True and this process is not already root, use `sudo -n` so we never block.
    - If the service is running as root, do not prepend sudo; this avoids false sudo failures.
    """
    use_sudo = bool(sudo and hasattr(os, "geteuid") and os.geteuid() != 0)
    cmd = (["sudo", "-n"] + argv) if use_sudo else argv
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        missing = "sudo" if use_sudo else argv[0]
        return 127, "", f"Command not found: {missing}"
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"

def _run_privileged(argv: List[str], timeout_s: int = 12) -> Tuple[int, str, str]:
    """Try a mutating system command in the safest practical order.

    Some Raspberry Pi images allow the service user to call NetworkManager over
    D-Bus without sudo. Others require sudo. Trying direct first avoids the
    previous failure mode where a valid non-root nmcli call was never attempted
    because `sudo -n` failed immediately.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return _run_cmd(argv, sudo=False, timeout_s=timeout_s)

    rc, out, err = _run_cmd(argv, sudo=False, timeout_s=timeout_s)
    if rc == 0:
        return rc, out, err

    direct_err = err or out
    rc2, out2, err2 = _run_cmd(argv, sudo=True, timeout_s=timeout_s)
    if rc2 == 0:
        return rc2, out2, err2

    combined = "; ".join(x for x in [direct_err, err2 or out2] if x)
    return rc2, out2, combined or NETWORK_PRIVILEGE_HELP

def _raise_privileged_error(err: str, context: str) -> None:
    text = (err or "").strip()
    if "password is required" in text.lower() or "not in the sudoers" in text.lower() or "permission denied" in text.lower() or "not authorized" in text.lower():
        raise RuntimeError(f"{context}: {text}\n\n{NETWORK_PRIVILEGE_HELP}")
    raise RuntimeError(text or context)
# ---------------- System helpers: network live status ----------------
def _live_ipv4_for_iface(iface: str) -> Optional[str]:
    """Return first global IPv4 address in CIDR form for iface (best effort)."""
    rc, out, _ = _run_cmd(["ip", "-4", "-o", "addr", "show", "dev", iface])
    if rc != 0 or not out:
        return None
    # Prefer scope global
    for ln in out.splitlines():
        if " scope global" in ln:
            m = re.search(r"\binet\s+(\S+)", ln)
            if m:
                return m.group(1)
    m = re.search(r"\binet\s+(\S+)", out)
    return m.group(1) if m else None

def _live_default_route() -> Tuple[Optional[str], Optional[str]]:
    """Return (iface, gateway) for the current default IPv4 route."""
    rc, out, _ = _run_cmd(["ip", "-4", "route", "show", "default"])
    if rc != 0 or not out:
        return None, None
    ln = out.splitlines()[0]
    m_dev = re.search(r"\bdev\s+(\S+)", ln)
    m_via = re.search(r"\bvia\s+(\S+)", ln)
    return (m_dev.group(1) if m_dev else None, m_via.group(1) if m_via else None)

def _live_gateway_for_iface(iface: str) -> Optional[str]:
    rc, out, _ = _run_cmd(["ip", "-4", "route", "show", "default", "dev", iface])
    if rc == 0 and out:
        m = re.search(r"\bvia\s+(\S+)", out)
        if m:
            return m.group(1)
    _if, gw = _live_default_route()
    return gw

def _dns_live_for_iface(iface: str) -> List[str]:
    """Best-effort DNS list: nmcli device, resolvectl, then resolv.conf."""
    if shutil.which("nmcli"):
        rc, out, _ = _run_cmd(["nmcli", "-t", "-g", "IP4.DNS", "device", "show", iface])
        if rc == 0 and out:
            vals = [ln.strip() for ln in out.splitlines() if ln.strip()]
            dns: List[str] = []
            for v in vals:
                dns.extend([x.strip() for x in v.replace(",", " ").split() if x.strip()])
            if dns:
                return dns
    if shutil.which("resolvectl"):
        rc, out, _ = _run_cmd(["resolvectl", "dns", iface])
        if rc == 0 and out:
            dns: List[str] = []
            for ln in out.splitlines():
                if ":" in ln:
                    rhs = ln.split(":", 1)[1]
                    dns.extend([x.strip() for x in rhs.replace(",", " ").split() if x.strip()])
            if dns:
                return dns
    return _dns_from_resolvconf()

def _dns_from_resolvconf() -> List[str]:
    """Parse /etc/resolv.conf; avoid returning only a local stub resolver when possible."""
    servers: List[str] = []
    try:
        for ln in Path("/etc/resolv.conf").read_text(errors="ignore").splitlines():
            ln = ln.strip()
            if not ln.startswith("nameserver"):
                continue
            parts = ln.split()
            if len(parts) >= 2:
                servers.append(parts[1])
    except Exception:
        return []
    # If systemd-resolved stub is present, prefer the upstream list
    if servers == ["127.0.0.53"] and Path("/run/systemd/resolve/resolv.conf").exists():
        try:
            servers = []
            for ln in Path("/run/systemd/resolve/resolv.conf").read_text(errors="ignore").splitlines():
                ln = ln.strip()
                if ln.startswith("nameserver"):
                    parts = ln.split()
                    if len(parts) >= 2:
                        servers.append(parts[1])
        except Exception:
            pass
    # Drop stub if mixed in
    servers = [s for s in servers if s != "127.0.0.53"]
    # de-dup while preserving order
    seen = set()
    out: List[str] = []
    for s in servers:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
def _nmcli_active_conn_for_iface(iface: str) -> Optional[str]:
    rc, out, _ = _run_cmd(["nmcli", "-t", "-f", "DEVICE,NAME", "con", "show", "--active"])
    if rc != 0 or not out:
        return None
    for ln in out.splitlines():
        # DEVICE:NAME
        if ":" not in ln:
            continue
        dev, name = ln.split(":", 1)
        if dev == iface and name:
            return name
    return None
def _nmcli_get_network(iface: str) -> Optional[Dict]:
    conn = _nmcli_active_conn_for_iface(iface)
    if not conn:
        return None

    # Determine config mode from the active connection
    rc, out, _ = _run_cmd(["nmcli", "-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns", "con", "show", conn])
    if rc != 0:
        return None

    fields: Dict[str, str] = {}
    for ln in out.splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        fields[k.strip()] = v.strip()

    method = fields.get("ipv4.method", "")
    mode = "dhcp" if method in ("auto", "shared") else ("manual" if method == "manual" else "dhcp")

    # Prefer configured values, but fall back to live status (DHCP commonly leaves these blank in con show)
    ip = (fields.get("ipv4.addresses") or "").split(",")[0].strip()
    if not ip:
        ip = _live_ipv4_for_iface(iface) or ""

    gw = (fields.get("ipv4.gateway") or "").strip()
    if not gw:
        gw = _live_gateway_for_iface(iface) or ""

    dns = (fields.get("ipv4.dns") or "").strip()
    dns_list = [d.strip() for d in dns.replace(",", " ").split() if d.strip()]
    if not dns_list:
        dns_list = _dns_live_for_iface(iface)

    return {"iface": iface, "mode": mode, "ipv4": ip or None, "gateway": gw or None, "dns": dns_list}

def _dhcpcd_conf_path() -> Path:
    return Path("/etc/dhcpcd.conf")
_MANAGED_START = "# --- tvh-bridge managed start ---"
_MANAGED_END = "# --- tvh-bridge managed end ---"
def _dhcpcd_get_network(iface: str) -> Dict:
    mode = "dhcp"
    ipv4 = None
    gateway = None
    dns: List[str] = _dns_from_resolvconf()
    # live ip/gw (best effort)
    rc, out, _ = _run_cmd(["ip", "-4", "-o", "addr", "show", "dev", iface])
    if rc == 0 and out:
        m = re.search(r"\binet\s+(\S+)", out)
        if m:
            ipv4 = m.group(1)
    rc, out, _ = _run_cmd(["ip", "route", "show", "default", "dev", iface])
    if rc == 0 and out:
        m = re.search(r"\bvia\s+(\S+)", out)
        if m:
            gateway = m.group(1)
    # config mode from file (prefer our managed block if present)
    conf = _dhcpcd_conf_path()
    try:
        txt = conf.read_text(errors="ignore")
        if _MANAGED_START in txt and _MANAGED_END in txt:
            block = txt.split(_MANAGED_START, 1)[1].split(_MANAGED_END, 1)[0]
            if re.search(r"^\s*interface\s+" + re.escape(iface) + r"\s*$", block, flags=re.M):
                mode = "manual" if ("static ip_address" in block) else "dhcp"
                m = re.search(r"static\s+ip_address=(\S+)", block)
                if m:
                    ipv4 = m.group(1)
                m = re.search(r"static\s+routers=(\S+)", block)
                if m:
                    gateway = m.group(1)
                m = re.search(r"static\s+domain_name_servers=(.+)", block)
                if m:
                    dns = [d.strip() for d in m.group(1).replace(",", " ").split() if d.strip()]
    except Exception:
        pass
    return {"iface": iface, "mode": mode, "ipv4": ipv4, "gateway": gateway, "dns": dns}
def _get_network_info() -> Tuple[Dict, List[str]]:
    warnings: List[str] = []
    # TeleTool exposes only eth0 in the web UI; keep discovery/status focused there.
    iface = "eth0"
    # Prefer NetworkManager if available
    if shutil.which("nmcli"):
        nm = _nmcli_get_network(iface)
        if nm:
            return nm, warnings
        warnings.append("nmcli detected but active connection not found for interface.")
    # Fallback: dhcpcd
    return _dhcpcd_get_network(iface), warnings
def _set_network_nmcli(iface: str, mode: str, ipv4: Optional[str], gateway: Optional[str], dns: List[str]) -> str:
    conn = _nmcli_active_conn_for_iface(iface)
    if not conn:
        raise RuntimeError(f"No active NetworkManager connection found for {iface}")
    if mode == "dhcp":
        # Apply the DHCP transition as ONE NetworkManager transaction.
        # Splitting this into several `nmcli con mod` calls can fail because
        # NetworkManager validates each intermediate state. For example:
        # - clearing addresses while a stale gateway remains is invalid
        # - changing to manual while addresses are empty is invalid
        # A single command lets NM validate the final DHCP state instead.
        cmds = [[
            "nmcli", "con", "mod", conn,
            "ipv4.method", "auto",
            "ipv4.addresses", "",
            "ipv4.gateway", "",
            "ipv4.dns", "",
            "ipv4.ignore-auto-dns", "no",
        ]]
    else:
        if not ipv4 or "/" not in ipv4:
            raise RuntimeError("Manual mode requires IPv4 address and subnet mask, e.g. 192.168.1.50 with subnet 255.255.255.0")
        # Apply the static transition as ONE transaction too, so NM never sees
        # a transient `manual` connection without an address/route.
        cmds = [[
            "nmcli", "con", "mod", conn,
            "ipv4.method", "manual",
            "ipv4.addresses", ipv4,
            "ipv4.gateway", gateway or "",
            "ipv4.dns", ",".join(dns) if dns else "",
            "ipv4.ignore-auto-dns", "yes" if dns else "no",
        ]]
    for argv in cmds:
        rc, _, err = _run_privileged(argv, timeout_s=12)
        if rc != 0:
            _raise_privileged_error(err, "NetworkManager update failed")

    # Apply to the currently active device. `device reapply` is less disruptive;
    # if it is unavailable/unsupported, fall back to bringing the connection up.
    rc, _, err = _run_privileged(["nmcli", "device", "reapply", iface], timeout_s=20)
    if rc != 0:
        rc, _, err = _run_privileged(["nmcli", "con", "up", conn], timeout_s=25)
        if rc != 0:
            _raise_privileged_error(err, "NetworkManager apply failed")
    return f"Updated NetworkManager connection '{conn}' and applied it to {iface}"
def _set_network_dhcpcd(iface: str, mode: str, ipv4: Optional[str], gateway: Optional[str], dns: List[str]) -> str:
    conf = _dhcpcd_conf_path()
    try:
        txt = conf.read_text(errors="ignore")
    except Exception as e:
        raise RuntimeError(f"Could not read {conf}: {e}")
    # Remove existing managed block
    if _MANAGED_START in txt and _MANAGED_END in txt:
        pre = txt.split(_MANAGED_START, 1)[0]
        post = txt.split(_MANAGED_END, 1)[1]
        txt = (pre.rstrip() + "\n\n" + post.lstrip()).strip() + "\n"
    if mode == "manual":
        if not ipv4 or "/" not in ipv4:
            raise RuntimeError("Manual mode requires IPv4 address and subnet mask, e.g. 192.168.1.50 with subnet 255.255.255.0")
        block = "\n".join(
            [
                _MANAGED_START,
                f"interface {iface}",
                f"static ip_address={ipv4}",
                f"static routers={gateway}" if gateway else "",
                f"static domain_name_servers={' '.join(dns)}" if dns else "",
                _MANAGED_END,
                "",
            ]
        )
        # drop blank lines created by optional fields
        block = "\n".join([ln for ln in block.splitlines() if ln.strip() != "" or ln.startswith("#")]) + "\n"
        txt = txt.rstrip() + "\n\n" + block
    else:
        # DHCP: no managed block needed
        txt = txt.rstrip() + "\n"
    # Write using sudo to avoid permission issues (won't hang due to -n)
    tmp = Path("/tmp/tvh_bridge_dhcpcd.conf")
    tmp.write_text(txt)
    rc, _, err = _run_privileged(["cp", str(tmp), str(conf)], timeout_s=8)
    if rc != 0:
        _raise_privileged_error(err, f"Failed to write {conf}")
    # restart service (best effort)
    rc, _, err = _run_privileged(["systemctl", "restart", "dhcpcd"], timeout_s=20)
    if rc != 0:
        # Don't hard-fail; config may still apply on next boot
        return f"Wrote {conf}, but failed to restart dhcpcd: {err or 'unknown error'}"
    return f"Updated {conf} and restarted dhcpcd"
def _set_network(iface: str, mode: str, ipv4: Optional[str], gateway: Optional[str], dns: List[str]) -> str:
    mode = mode.lower().strip()
    if mode not in ("dhcp", "manual"):
        raise RuntimeError("mode must be 'dhcp' or 'manual'")
    # Prefer NM if present + active connection exists
    if shutil.which("nmcli") and _nmcli_active_conn_for_iface(iface):
        return _set_network_nmcli(iface, mode, ipv4, gateway, dns)
    return _set_network_dhcpcd(iface, mode, ipv4, gateway, dns)
def _get_persistent_hostname() -> str:
    """
    Prefer /etc/hostname (persisted) and fall back to socket.gethostname().
    """
    try:
        p = Path("/etc/hostname")
        if p.exists():
            v = p.read_text(encoding="utf-8", errors="ignore").strip()
            if v:
                return v
    except Exception:
        pass
    try:
        return socket.gethostname()
    except Exception:
        return ""
def _get_runtime_hostname() -> str:
    """Return the currently active hostname (may differ from /etc/hostname until reboot)."""
    try:
        rc, out, _ = _run_cmd(["hostname"], sudo=False, timeout_s=3)
        if rc == 0:
            v = (out or "").strip()
            if v:
                return v
    except Exception:
        pass
    try:
        return socket.gethostname()
    except Exception:
        return ""
def _sudo_write_text(dest: Path, content: str, tmp_basename: str) -> None:
    """Write file content using a temp file + `sudo -n cp` (never prompts for a password)."""
    tmp = Path("/tmp") / tmp_basename
    tmp.write_text(content, encoding="utf-8")
    rc, _, err = _run_cmd(["cp", str(tmp), str(dest)], sudo=True, timeout_s=8)
    try:
        tmp.unlink()
    except Exception:
        pass
    if rc != 0:
        raise RuntimeError(err or f"Failed to write {dest} (sudo required)")
def _update_hosts_127001(new_hostname: str) -> None:
    """Ensure /etc/hosts has a 127.0.1.1 entry matching the hostname."""
    hosts = Path("/etc/hosts")
    txt = ""
    try:
        txt = hosts.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        # if we can't read normally, try via sudo cat
        rc, out, err = _run_cmd(["cat", str(hosts)], sudo=True, timeout_s=6)
        if rc != 0:
            raise RuntimeError(err or "Could not read /etc/hosts")
        txt = out
    lines = txt.splitlines()
    out_lines = []
    found = False
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            out_lines.append(ln)
            continue
        if s.startswith("127.0.1.1"):
            out_lines.append(f"127.0.1.1	{new_hostname}")
            found = True
        else:
            out_lines.append(ln)
    if not found:
        out_lines.append(f"127.0.1.1	{new_hostname}")
    _sudo_write_text(hosts, "\n".join(out_lines) + "\n", "hosts.tmp")
class NetworkUpdateReq(BaseModel):
    mode: str = Field(pattern="^(dhcp|manual)$")
    # Interface is intentionally not exposed in the UI; TeleTool manages eth0 only.
    # Keep iface/ipv4 for backwards-compatible API callers, but api_system_network
    # always applies to eth0.
    iface: Optional[str] = None
    # New UI fields for manual mode
    ip_address: Optional[str] = None  # e.g. 192.168.1.50
    subnet_prefix: Optional[int] = None  # legacy UI/API, e.g. 24
    subnet_mask: Optional[str] = None  # e.g. 255.255.255.0
    # Backwards-compatible CIDR form
    ipv4: Optional[str] = None  # e.g. 192.168.1.50/24
    gateway: Optional[str] = None
    dns: Optional[str] = None  # space/comma separated
class HostnameReq(BaseModel):
    hostname: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9-]*$")
def _cloud_init_present() -> bool:
    return Path("/etc/cloud/cloud.cfg").exists() or Path("/etc/cloud/cloud.cfg.d").exists()

def _ensure_cloud_init_preserve_hostname() -> bool:
    """If cloud-init is present (common on netplan/Ubuntu images), ensure it does NOT reset hostname on boot.

    We do this by writing a late config snippet:
      /etc/cloud/cloud.cfg.d/99-webui-preserve-hostname.cfg
    containing:
      preserve_hostname: true

    Returns True if cloud-init was detected (and we attempted to enforce preserve), else False.
    """
    if not _cloud_init_present():
        return False

    cloud_d = Path("/etc/cloud/cloud.cfg.d")
    # Ensure directory exists (may require sudo)
    if not cloud_d.exists():
        rc, _, err = _run_cmd(["mkdir", "-p", str(cloud_d)], sudo=True, timeout_s=8)
        if rc != 0:
            raise RuntimeError(err or "Failed to create /etc/cloud/cloud.cfg.d (sudo required)")

    content = "preserve_hostname: true\n"
    _sudo_write_text(cloud_d / "99-webui-preserve-hostname.cfg", content, "cloudhost.tmp")
    return True

# ---- System: hostname + network (keep endpoints light) ----

# Simple cache for network discovery (subprocess-heavy on some images)
_NETINFO_CACHE: Dict[str, object] = {"ts": 0.0, "net": None, "warnings": []}
_NETINFO_CACHE_TTL_S: float = float(cfg.get("netinfo_cache_ttl_s", 10.0))

def _get_network_info_cached() -> Tuple[Dict, List[str]]:
    now = time.time()
    ts = float(_NETINFO_CACHE.get("ts") or 0.0)
    net = _NETINFO_CACHE.get("net")
    warnings = _NETINFO_CACHE.get("warnings") or []
    if net is not None and (now - ts) < _NETINFO_CACHE_TTL_S:
        return net, list(warnings)
    net2, warnings2 = _get_network_info()
    _NETINFO_CACHE["ts"] = now
    _NETINFO_CACHE["net"] = net2
    _NETINFO_CACHE["warnings"] = list(warnings2)
    return net2, list(warnings2)

@app.get("/api/system/hostname")
def api_system_hostname_get():
    """Return hostname info (no realtime resource monitoring)."""
    hn_persisted = _get_persistent_hostname()
    hn_runtime = _get_runtime_hostname()
    return {
        "hostname": hn_persisted,
        "hostname_detail": {"persisted": hn_persisted, "runtime": hn_runtime},
    }

@app.get("/api/system/network_info")
def api_system_network_info():
    net, warnings = _get_network_info_cached()
    return {"network": net, "warnings": warnings}



@app.post("/api/system/restart_program")
def api_system_restart_program():
    # Avoid permission issues: we don't try to call systemd here.
    # Instead, exit the process; if managed by systemd, it will restart.
    _schedule_program_restart(0.75)
    return {"ok": True, "message": "Program restart requested (process exiting)."}


class ProgramUpdateReq(BaseModel):
    confirm: bool = False


@app.get("/api/system/update_status")
def api_system_update_status():
    return _update_status_snapshot()


@app.post("/api/system/update_from_server")
def api_system_update_from_server(req: ProgramUpdateReq):
    if not req.confirm:
        raise HTTPException(400, "Confirmation is required before updating from server")
    current = _update_status_snapshot()
    if current.get("running"):
        raise HTTPException(409, "Update is already running")

    status = _set_update_status(
        running=True,
        done=False,
        percent=1,
        step="Starting update",
        error=None,
        started_at=int(time.time()),
        finished_at=None,
        stats=None,
    )
    threading.Thread(target=_run_program_update_worker, name="program-update-worker", daemon=True).start()
    return {"ok": True, "message": "Update started.", "status": status}


@app.post("/api/system/reboot")
def api_system_reboot():
    """Reboot the Pi using the same privilege fallback as network changes.

    The previous implementation only tried `sudo -n reboot`. That fails on many
    Raspberry Pi installs because the TeleTool sudoers helper only granted
    network commands, and some systems prefer `systemctl reboot`/`shutdown -r
    now` over the bare `reboot` command. Try the common reboot commands in a
    safe non-interactive order and return the real error if none are permitted.
    """
    attempts = [
        ["systemctl", "reboot"],
        ["shutdown", "-r", "now"],
        ["reboot"],
    ]
    errors: List[str] = []
    for argv in attempts:
        rc, _, err = _run_privileged(argv, timeout_s=8)
        if rc == 0:
            return {"ok": True, "message": "Reboot requested."}
        errors.append(f"{' '.join(argv)}: {err or 'failed'}")

    detail = "; ".join(errors) or "unknown error"
    raise HTTPException(
        403,
        "Reboot not permitted. Run install_network_privileges.sh once, or configure "
        "passwordless sudo for systemctl reboot, shutdown -r now, or reboot. "
        f"Details: {detail}",
    )
def _prefix_from_subnet_mask(mask: str) -> int:
    """Convert dotted IPv4 subnet mask such as 255.255.255.0 to CIDR prefix length."""
    mask_s = str(mask or "").strip()
    try:
        addr = ipaddress.IPv4Address(mask_s)
    except Exception:
        raise RuntimeError("Manual mode requires a valid subnet mask, e.g. 255.255.255.0")
    bits = bin(int(addr))[2:].zfill(32)
    if "01" in bits:
        raise RuntimeError("Subnet mask must be contiguous, e.g. 255.255.255.0")
    prefix = bits.count("1")
    if prefix < 1 or prefix > 32:
        raise RuntimeError("Subnet mask must represent a prefix between /1 and /32, e.g. 255.255.255.0")
    return prefix

def _cidr_from_network_request(req: NetworkUpdateReq) -> Optional[str]:
    """Build validated CIDR from UI fields, falling back to legacy ipv4."""
    if req.mode != "manual":
        return None
    ip_s = str(req.ip_address or "").strip()
    mask_s = str(req.subnet_mask or "").strip()
    prefix = req.subnet_prefix

    if ip_s or mask_s or prefix is not None:
        if not ip_s:
            raise RuntimeError("Manual mode requires an IP address.")
        try:
            ipaddress.IPv4Address(ip_s)
        except Exception:
            raise RuntimeError("Manual mode requires a valid IPv4 address, e.g. 192.168.1.50")

        if mask_s:
            prefix_i = _prefix_from_subnet_mask(mask_s)
        else:
            try:
                prefix_i = int(prefix)
            except Exception:
                raise RuntimeError("Manual mode requires a subnet mask, e.g. 255.255.255.0")
            if prefix_i < 1 or prefix_i > 32:
                raise RuntimeError("Subnet mask converted to an invalid prefix; use a mask such as 255.255.255.0")
        return f"{ip_s}/{prefix_i}"

    legacy = str(req.ipv4 or "").strip()
    if not legacy:
        raise RuntimeError("Manual mode requires an IP address and subnet mask.")
    try:
        ipaddress.IPv4Interface(legacy)
    except Exception:
        raise RuntimeError("Manual mode requires IPv4 CIDR form, e.g. 192.168.1.50/24")
    return legacy


@app.post("/api/system/network")
def api_system_network(req: NetworkUpdateReq):
    # TeleTool appliance networking is intentionally eth0-only. Ignore any legacy
    # iface value from old clients so the UI cannot accidentally alter Wi-Fi/lo/etc.
    iface = "eth0"
    dns_list: List[str] = []
    if req.dns:
        dns_list = [d.strip() for d in req.dns.replace(",", " ").split() if d.strip()]
    ipv4 = _cidr_from_network_request(req) if req.mode == "manual" else None
    gateway = req.gateway if req.mode == "manual" else None
    try:
        msg = _set_network(iface=iface, mode=req.mode, ipv4=ipv4, gateway=gateway, dns=dns_list)
        return {"ok": True, "message": msg}
    except Exception as e:
        raise HTTPException(403, f"{e}")
@app.post("/api/system/hostname")
def api_system_hostname(req: HostnameReq):
    """
    Change hostname without getting stuck on permission prompts.
    We try `hostnamectl set-hostname` first (systemd systems). If that isn't
    available or doesn't persist, we fall back to updating /etc/hostname and
    /etc/hosts via sudo-copy, then apply runtime hostname best-effort.
    """
    hn = (req.hostname or "").strip()
    cloudinit = False
    # 1) Try hostnamectl (preferred on systemd)
    used_hostnamectl = False
    if shutil.which("hostnamectl"):
        rc, _, err = _run_cmd(["hostnamectl", "set-hostname", hn], sudo=True, timeout_s=8)
        if rc == 0:
            used_hostnamectl = True
        else:
            # Keep going to fallback, but remember the failure for error reporting if fallback can't run.
            hostnamectl_err = err or "unknown error"
    else:
        hostnamectl_err = "hostnamectl not found"
    # 2) Fallback: write /etc/hostname + /etc/hosts (persisted on boot)
    #    (We do this even after hostnamectl, because some images/overlays behave oddly.)
    try:
        _sudo_write_text(Path("/etc/hostname"), hn + "\n", "hostname.tmp")
        _update_hosts_127001(hn)
        cloudinit = _ensure_cloud_init_preserve_hostname()
    except Exception as e:
        # If hostnamectl worked, treat fallback as non-fatal.
        if not used_hostnamectl:
            raise HTTPException(
                403,
                "Hostname change not permitted. Configure passwordless sudo for 'hostnamectl set-hostname' "
                "and/or sudo cp to /etc/hostname and /etc/hosts. "
                f"Details: {e} (hostnamectl: {hostnamectl_err})",
            )
    # 3) Apply runtime hostname best-effort (doesn't block)
    _run_cmd(["hostname", hn], sudo=True, timeout_s=4)
    # mDNS/Avahi sometimes needs a restart to advertise the new name
    if shutil.which("systemctl"):
        _run_cmd(["systemctl", "restart", "avahi-daemon"], sudo=True, timeout_s=6)
    # 4) Verify persisted (read /etc/hostname) and runtime hostname
    persisted = _get_persistent_hostname()
    runtime = _get_runtime_hostname()
    # Double-check /etc/hostname directly (in case of odd permission overlays)
    rc, out, _ = _run_cmd(["cat", "/etc/hostname"], sudo=True, timeout_s=5)
    etc_hn = (out or "").strip() if rc == 0 else ""
    if persisted != hn or (etc_hn and etc_hn != hn):
        got = persisted or etc_hn or "(empty)"
        raise HTTPException(
            500,
            f"Hostname did not persist (expected '{hn}', got '{got}'). "
            "If you are using a read-only/overlay root filesystem, hostname may reset on reboot.",
        )
    return {
        "ok": True,
        "cloud_init_detected": cloudinit,
        "hostname": persisted,
        "hostname_detail": {"persisted": persisted, "runtime": runtime},
        "message": f"Hostname set to '{persisted}'.",
    }

# ---------------- Graceful shutdown ----------------
@app.on_event("shutdown")
def _shutdown():
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
