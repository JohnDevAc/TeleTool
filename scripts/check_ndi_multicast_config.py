#!/usr/bin/env python3
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ndi_runtime_config import ndi_runtime_settings, write_ndi_runtime_config


def expect_value_error(config, message):
    try:
        ndi_runtime_settings(config)
    except ValueError:
        return
    raise AssertionError(message)


def main():
    defaults = ndi_runtime_settings({})
    assert defaults["ndi_multicast_enabled"] is False
    assert defaults["ndi_multicast_netprefix"] == "239.255.0.0"
    assert defaults["ndi_multicast_netmask"] == "255.255.0.0"
    assert defaults["ndi_multicast_ttl"] == 1

    legacy = ndi_runtime_settings(
        {
            "ndi_multicast_enabled": True,
            "ndi_multicast_addr": "239.250.0.0",
            "ndi_multicast_netmask": "255.255.0.0",
            "ndi_multicast_ttl": 4,
        }
    )
    assert legacy["ndi_multicast_netprefix"] == "239.250.0.0"
    assert legacy["ndi_multicast_ttl"] == 4

    expect_value_error(
        {
            "ndi_multicast_enabled": True,
            "ndi_multicast_netprefix": "192.168.0.0",
        },
        "Unicast prefixes must be rejected",
    )
    expect_value_error(
        {
            "ndi_multicast_enabled": True,
            "ndi_multicast_netprefix": "239.255.1.0",
            "ndi_multicast_netmask": "255.255.0.0",
        },
        "A prefix inside, rather than at the start of, its range must be rejected",
    )
    expect_value_error(
        {
            "ndi_multicast_enabled": True,
            "ndi_multicast_netprefix": "239.255.0.0",
            "ndi_multicast_netmask": "255.0.255.0",
        },
        "Non-contiguous masks must be rejected",
    )
    expect_value_error(
        {
            "ndi_multicast_enabled": True,
            "ndi_multicast_netprefix": "239.255.0.0",
            "ndi_multicast_ttl": 0,
        },
        "TTL zero must be rejected",
    )

    old_dir = os.environ.get("NDI_CONFIG_DIR")
    old_path = os.environ.get("TELETOOL_NDI_CONFIG_PATH")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ndi-config.v1.json"
            path.write_text(
                json.dumps(
                    {
                        "ndi": {
                            "networks": {"custom": "preserved"},
                            "multicast": {"recv": {"enable": True}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            os.environ["NDI_CONFIG_DIR"] = temp_dir
            os.environ["TELETOOL_NDI_CONFIG_PATH"] = str(path)
            settings = write_ndi_runtime_config(
                {
                    "ndi_groups": "Studio",
                    "ndi_discovery_server": "192.168.0.105",
                    "ndi_multicast_enabled": True,
                    "ndi_multicast_netprefix": "239.250.0.0",
                    "ndi_multicast_netmask": "255.255.0.0",
                    "ndi_multicast_ttl": 3,
                }
            )
            assert settings["ndi_multicast_enabled"] is True
            root = json.loads(path.read_text(encoding="utf-8"))
            ndi = root["ndi"]
            assert ndi["networks"]["custom"] == "preserved"
            assert ndi["networks"]["discovery"] == "192.168.0.105"
            assert ndi["groups"] == {"send": "Studio", "recv": "Studio"}
            assert ndi["multicast"]["recv"] == {"enable": True}
            assert ndi["multicast"]["send"] == {
                "enable": True,
                "netprefix": "239.250.0.0",
                "netmask": "255.255.0.0",
                "ttl": 3,
            }
    finally:
        if old_dir is None:
            os.environ.pop("NDI_CONFIG_DIR", None)
        else:
            os.environ["NDI_CONFIG_DIR"] = old_dir
        if old_path is None:
            os.environ.pop("TELETOOL_NDI_CONFIG_PATH", None)
        else:
            os.environ["TELETOOL_NDI_CONFIG_PATH"] = old_path

    print("NDI multicast runtime configuration checks passed")


if __name__ == "__main__":
    main()
