#!/usr/bin/env python3
"""Opt-in hardware check; temporarily replaces and restores an active test card."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="TeleTool HTTP origin")
    parser.add_argument("--interrupt-test-card", action="store_true", required=True)
    parser.add_argument("--audio-sink", choices=("alsa-null", "clocked-fake"), default="alsa-null")
    args = parser.parse_args()

    def api(path, body=None):
        request = urllib.request.Request(
            args.device.rstrip("/") + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    original = api("/api/status?lite=1&stats=1&logs=0&rf=0")
    original_audio = api("/api/audio/status?logs=0")
    original_config = api("/api/config/ui")
    assert original["running"] and original["source_mode"] == "test_card", "An active test card is required"
    restore = {key: original[key] for key in (
        "ndi_name", "ndi_groups", "ndi_multicast_enabled", "ndi_multicast_netprefix",
        "ndi_multicast_netmask", "ndi_multicast_ttl",
    ) if original.get(key) is not None}
    results = []
    with tempfile.TemporaryDirectory(prefix="teletool-ndi-timing-") as directory:
        os.environ["NDI_CONFIG_DIR"] = directory
        os.environ["TELETOOL_NDI_CONFIG_PATH"] = str(Path(directory) / "ndi-config.v1.json")
        from gst_ndi import GstNDIBridge

        config = json.loads((ROOT / "config.example.json").read_text())
        config.update(original_config)
        config.update(ndi_test_card_width=1920, ndi_test_card_height=1080, ndi_test_card_fps=60)
        bridge = GstNDIBridge(config)

        def ticks():
            fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
            return int(fields[11]) + int(fields[12])

        def measure(label):
            time.sleep(2)
            start = time.monotonic()
            cpu = ticks()
            samples = []
            for _ in range(10):
                samples.append(bridge.status_lite(include_stats=True))
                time.sleep(1)
            elapsed = time.monotonic() - start
            status = bridge.status_lite(include_stats=True)
            rss = next(line.strip() for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("VmRSS:"))
            record = {"label": label, "cpu_percent_one_core": (ticks() - cpu) / os.sysconf("SC_CLK_TCK") / elapsed * 100,
                      "rss": rss, "status": status, "audio": bridge.lineout_status(False)}
            print(json.dumps(record), flush=True)
            results.append(record)
            assert status["running"] and not status["last_error"], status
            assert status["ndi_dropped"] == 0
            rates = [s["ndi_fps_est"] for s in samples if s.get("ndi_fps_est") is not None]
            assert rates and min(rates) > 55, rates

        null_output = {"id": "alsa:null", "device": "null", "sink": "alsasink", "kind": "alsa", "label": "ALSA null validation"}
        normal_builder = bridge._build_lineout_pipeline_desc

        def measured_output(*a, **kw):
            description = normal_builder(*a, **kw)
            if args.audio_sink == "clocked-fake":
                # Keep the real source/conversion chain; substitute the terminal
                # sink only. ALSA null has no hardware clock and can spin a core.
                description = description.rsplit("!", 1)[0] + "! fakesink name=lineoutsink async=false sync=true"
            return description
        api("/api/test-card/stop", {})
        try:
            for delay in (100, 500):
                bridge.start_with_delay("test-card://local", restore["ndi_name"], source_mode="test_card", delay_ms=delay,
                                        **{k: v for k, v in restore.items() if k != "ndi_name"})
                measure(f"1080p60 card, {delay} ms, NDI only")
                with patch.object(bridge, "_resolve_audio_output_device", return_value=null_output), \
                     patch.object(bridge, "_build_lineout_pipeline_desc", side_effect=measured_output):
                    bridge.lineout_start("alsa:null", 0.8)
                    measure(f"1080p60 card, {delay} ms, NDI + {args.audio_sink}")
                    assert bridge.lineout_status(False)["running"]
                    bridge.lineout_stop()
                assert bridge.status()["running"]
                bridge.stop()

            # Real sink-open failure, plus PTP loss through the normal monitor.
            bridge.start_with_delay("test-card://local", restore["ndi_name"], source_mode="test_card", delay_ms=500,
                                    **{k: v for k, v in restore.items() if k != "ndi_name"})
            bad = dict(null_output, device="hw:99,99")
            with patch.object(bridge, "_resolve_audio_output_device", return_value=bad):
                try:
                    bridge.lineout_start()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Missing ALSA device was accepted")
            assert bridge.status()["running"]
            with patch.object(bridge, "_resolve_audio_output_device", return_value=dict(null_output, kind="inferno")):
                with patch.object(bridge, "_inferno_clock_status", return_value={"ready": False, "details": "Test: no PTP leader"}):
                    try:
                        bridge.lineout_start()
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("PTP-unready start was accepted")
                clock = {"ready": True}
                with patch.object(bridge, "_inferno_clock_status", side_effect=lambda **kw: clock):
                    bridge.lineout_start()
                    clock = {"ready": False, "details": "Test: PTP leader lost"}
                    time.sleep(2)
                    assert not bridge.lineout_status(False)["running"]
                    assert "PTP leader lost" in bridge.lineout_status(False)["last_error"]
            assert bridge.status()["running"] and not bridge.status()["last_error"]
            print("Missing-device and PTP-unready/loss isolation checks passed.", flush=True)
        finally:
            bridge.stop()
            api("/api/test-card/start", restore)
            if original_audio["running"]:
                api("/api/audio/start", {"device_id": original_audio["device_id"], "volume": original_audio["volume"]})
            assert api("/api/config/ui") == original_config, "Device configuration changed during validation"
            print("Original test card and audio state restored.", flush=True)


if __name__ == "__main__":
    main()
