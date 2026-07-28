import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


NDI_CONFIG_FILENAME = "ndi-config.v1.json"
DEFAULT_NDI_MULTICAST_NETPREFIX = "239.255.0.0"
DEFAULT_NDI_MULTICAST_NETMASK = "255.255.0.0"
DEFAULT_NDI_MULTICAST_TTL = 1


def normalise_ndi_groups(value: Any) -> str:
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


def normalise_ndi_discovery_servers(value: Any) -> str:
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


def normalise_ndi_multicast_settings(
    *,
    enabled: Any,
    netprefix: Any,
    netmask: Any,
    ttl: Any,
) -> Dict[str, Any]:
    if isinstance(enabled, str):
        enabled_value = enabled.strip().lower() in {"1", "true", "yes", "on"}
    else:
        enabled_value = bool(enabled)

    prefix_text = str(netprefix or "").strip()
    if not prefix_text:
        if enabled_value:
            raise ValueError("Enter an NDI multicast address prefix")
        prefix_text = DEFAULT_NDI_MULTICAST_NETPREFIX
    try:
        prefix = ipaddress.IPv4Address(prefix_text)
    except ipaddress.AddressValueError:
        raise ValueError("Enter a valid IPv4 multicast address prefix")
    if not prefix.is_multicast:
        raise ValueError("The NDI multicast address prefix must be between 224.0.0.0 and 239.255.255.255")

    mask_text = str(netmask or "").strip()
    if not mask_text:
        if enabled_value:
            raise ValueError("Enter an NDI multicast network mask")
        mask_text = DEFAULT_NDI_MULTICAST_NETMASK
    try:
        mask_network = ipaddress.IPv4Network(f"0.0.0.0/{mask_text}")
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        raise ValueError("Enter a valid contiguous IPv4 multicast network mask")
    canonical_mask = str(mask_network.netmask)
    multicast_network = ipaddress.IPv4Network((str(prefix), canonical_mask), strict=False)
    if prefix != multicast_network.network_address:
        raise ValueError("The NDI multicast address prefix must be the first address in its masked range")
    if not multicast_network.broadcast_address.is_multicast:
        raise ValueError("The NDI multicast prefix and mask must describe only multicast addresses")
    if multicast_network.num_addresses < 4:
        raise ValueError("The NDI multicast range must contain at least four addresses")

    try:
        ttl_value = int(ttl)
    except (TypeError, ValueError):
        raise ValueError("NDI multicast TTL must be a whole number")
    if ttl_value < 1 or ttl_value > 255:
        raise ValueError("NDI multicast TTL must be between 1 and 255")

    return {
        "ndi_multicast_enabled": enabled_value,
        "ndi_multicast_netprefix": str(prefix),
        "ndi_multicast_netmask": canonical_mask,
        "ndi_multicast_ttl": ttl_value,
    }


def ndi_runtime_settings(
    config: Dict[str, Any],
    *,
    ndi_groups: Optional[str] = None,
    ndi_multicast_enabled: Optional[bool] = None,
    ndi_multicast_netprefix: Optional[str] = None,
    ndi_multicast_netmask: Optional[str] = None,
    ndi_multicast_ttl: Optional[int] = None,
) -> Dict[str, Any]:
    groups_value = config.get("ndi_groups", "") if ndi_groups is None else ndi_groups
    prefix_default = config.get("ndi_multicast_netprefix")
    if not str(prefix_default or "").strip():
        prefix_default = config.get("ndi_multicast_addr") or DEFAULT_NDI_MULTICAST_NETPREFIX
    multicast = normalise_ndi_multicast_settings(
        enabled=config.get("ndi_multicast_enabled", False)
        if ndi_multicast_enabled is None
        else ndi_multicast_enabled,
        netprefix=prefix_default if ndi_multicast_netprefix is None else ndi_multicast_netprefix,
        netmask=config.get("ndi_multicast_netmask", DEFAULT_NDI_MULTICAST_NETMASK)
        if ndi_multicast_netmask is None
        else ndi_multicast_netmask,
        ttl=config.get("ndi_multicast_ttl", DEFAULT_NDI_MULTICAST_TTL)
        if ndi_multicast_ttl is None
        else ndi_multicast_ttl,
    )
    return {
        "ndi_groups": normalise_ndi_groups(groups_value),
        "ndi_discovery_server": normalise_ndi_discovery_servers(config.get("ndi_discovery_server", "")),
        **multicast,
    }


def ndi_config_dir() -> Path:
    configured_dir = str(os.environ.get("NDI_CONFIG_DIR") or "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser()

    configured_path = str(os.environ.get("TELETOOL_NDI_CONFIG_PATH") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser().parent

    home = str(os.environ.get("HOME") or "").strip()
    if home:
        return Path(home).expanduser() / ".ndi"
    return Path.home() / ".ndi"


def ndi_config_path() -> Path:
    configured_path = str(os.environ.get("TELETOOL_NDI_CONFIG_PATH") or "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    return ndi_config_dir() / NDI_CONFIG_FILENAME


def configure_ndi_environment() -> Path:
    directory = ndi_config_dir()
    os.environ.setdefault("NDI_CONFIG_DIR", str(directory))
    os.environ.setdefault("TELETOOL_NDI_CONFIG_PATH", str(directory / NDI_CONFIG_FILENAME))
    return directory


def write_ndi_runtime_config(
    config: Dict[str, Any],
    *,
    ndi_groups: Optional[str] = None,
    ndi_multicast_enabled: Optional[bool] = None,
    ndi_multicast_netprefix: Optional[str] = None,
    ndi_multicast_netmask: Optional[str] = None,
    ndi_multicast_ttl: Optional[int] = None,
) -> Dict[str, Any]:
    configure_ndi_environment()
    settings = ndi_runtime_settings(
        config,
        ndi_groups=ndi_groups,
        ndi_multicast_enabled=ndi_multicast_enabled,
        ndi_multicast_netprefix=ndi_multicast_netprefix,
        ndi_multicast_netmask=ndi_multicast_netmask,
        ndi_multicast_ttl=ndi_multicast_ttl,
    )
    groups_value = settings["ndi_groups"]
    discovery_value = settings["ndi_discovery_server"]
    path = ndi_config_path()

    root: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                root = loaded
        except Exception:
            root = {}

    ndi = root.get("ndi")
    if not isinstance(ndi, dict):
        ndi = {}
        root["ndi"] = ndi

    networks = ndi.get("networks")
    if not isinstance(networks, dict):
        networks = {}
    if discovery_value:
        networks["discovery"] = discovery_value
    else:
        networks.pop("discovery", None)
    if networks:
        ndi["networks"] = networks
    else:
        ndi.pop("networks", None)

    groups = ndi.get("groups")
    if not isinstance(groups, dict):
        groups = {}
    if groups_value:
        groups["send"] = groups_value
        groups["recv"] = groups_value
    else:
        groups.pop("send", None)
        groups.pop("recv", None)
    if groups:
        ndi["groups"] = groups
    else:
        ndi.pop("groups", None)

    multicast = ndi.get("multicast")
    if not isinstance(multicast, dict):
        multicast = {}
    multicast_send = multicast.get("send")
    if not isinstance(multicast_send, dict):
        multicast_send = {}
    multicast_send.update(
        {
            "enable": settings["ndi_multicast_enabled"],
            "netprefix": settings["ndi_multicast_netprefix"],
            "netmask": settings["ndi_multicast_netmask"],
            "ttl": settings["ndi_multicast_ttl"],
        }
    )
    multicast["send"] = multicast_send
    ndi["multicast"] = multicast

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

    return settings
