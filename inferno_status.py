import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping


_PORT_STATE_NAMES = {
    1: "Initializing",
    2: "Faulty",
    3: "Disabled",
    4: "Listening",
    5: "PreMaster",
    6: "Master",
    7: "Passive",
    8: "Uncalibrated",
    9: "Slave",
}
_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: Dict[str, Dict[str, Any]] = {}


def _port_state_name(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return _PORT_STATE_NAMES.get(value, str(value))
    text = str(value or "").strip()
    if text.isdigit():
        return _PORT_STATE_NAMES.get(int(text), text)
    return text


def inferno_clock_status_from_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    instance = payload.get("instance")
    if not isinstance(instance, Mapping):
        return {
            "ready": False,
            "state": "Unknown",
            "details": "Inferno PTP status is unavailable. Try again shortly.",
        }

    port_data = instance.get("port_ds")
    ports = port_data if isinstance(port_data, list) else []
    states = [
        _port_state_name(port.get("port_state"))
        for port in ports
        if isinstance(port, Mapping)
    ]
    states = [state for state in states if state]

    if any(state.lower() == "slave" for state in states):
        return {
            "ready": True,
            "state": "Slave",
            "details": "PTP synchronized to the network primary leader.",
        }

    if any(state.lower() == "uncalibrated" for state in states):
        return {
            "ready": False,
            "state": "Uncalibrated",
            "details": "A PTP primary leader was detected, but clock synchronization is still in progress.",
        }

    if any(state.lower() in {"listening", "master", "premaster"} for state in states):
        state = next(
            state
            for state in states
            if state.lower() in {"listening", "master", "premaster"}
        )
        return {
            "ready": False,
            "state": state,
            "details": (
                "No PTP grandmaster or primary leader is available. "
                "Connect one to the audio network and try again."
            ),
        }

    state = ", ".join(states) if states else "Unknown"
    return {
        "ready": False,
        "state": state,
        "details": f"Inferno PTP clock is not synchronized (state: {state}).",
    }


def _read_inferno_clock_status(
    clock_path: str,
    observation_path: str,
    timeout_s: float,
) -> Dict[str, Any]:
    if not Path(clock_path).exists():
        return {
            "ready": False,
            "state": "Unavailable",
            "details": "Inferno clock service is not ready.",
        }
    if not Path(observation_path).exists():
        return {
            "ready": False,
            "state": "Unavailable",
            "details": "Inferno PTP status is unavailable. Restart the Inferno clock service and try again.",
        }

    try:
        chunks = []
        total = 0
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(max(0.05, float(timeout_s)))
            sock.connect(observation_path)
            while total < 65536:
                chunk = sock.recv(min(16384, 65536 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("PTP observation payload is not an object")
        return inferno_clock_status_from_payload(payload)
    except Exception:
        return {
            "ready": False,
            "state": "Unavailable",
            "details": "Inferno PTP status could not be read. Try again shortly.",
        }


def inferno_clock_status(
    clock_path: str,
    observation_path: str,
    *,
    timeout_s: float = 0.2,
    max_age_s: float = 1.0,
    force: bool = False,
) -> Dict[str, Any]:
    cache_key = f"{clock_path}\0{observation_path}"
    now = time.monotonic()
    if not force:
        with _CACHE_LOCK:
            cached = _STATUS_CACHE.get(cache_key)
            if cached and now - float(cached["checked_at"]) <= max(0.0, float(max_age_s)):
                return dict(cached["status"])

    status = _read_inferno_clock_status(clock_path, observation_path, timeout_s)
    with _CACHE_LOCK:
        _STATUS_CACHE[cache_key] = {
            "checked_at": time.monotonic(),
            "status": dict(status),
        }
    return status
