import time
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from requests.auth import AuthBase
from urllib3.util.retry import Retry


TELETOOL_UK_AUTO_SCANFILE = "teletool/uk-auto-dvbt-dvbt2"
TELETOOL_UK_AUTO_LABEL = "United Kingdom: auto DVB-T/T2 (TeleTool, slower)"


class TvheadendClient:
    """Small Tvheadend API client with lightweight TTL caching.

    Why:
    - Channel lists and playlists are expensive to fetch repeatedly from the UI poller.
    - Using sync `requests` is fine as long as FastAPI endpoints that call it are sync too.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: Union[int, float] = 10,
        cache_ttl_s: float = 10.0,
        *,
        # Reliability knobs
        connect_timeout_s: Union[int, float] = 3,
        retries: int = 3,
        backoff_s: float = 0.4,
        auth: Optional[Union[Tuple[str, str], AuthBase]] = None,
        verify_tls: bool = True,
        pool_maxsize: int = 10,
    ):
        """Create a Tvheadend API client.

        Notes on reliability:
        - We use a Session + connection pooling for performance.
        - We also mount an HTTPAdapter with urllib3 Retry to survive transient
          TCP resets / 502/503/504s and short TVH restarts.
        """

        self.base_url = base_url.rstrip("/")
        self.cache_ttl_s = float(cache_ttl_s)

        # requests accepts a (connect, read) timeout tuple.
        self._timeout = (float(connect_timeout_s), float(timeout_s))
        self._verify_tls = bool(verify_tls)
        self._auth = auth

        self._retries = int(retries)
        self._backoff_s = float(backoff_s)
        self._pool_maxsize = int(pool_maxsize)
        self._session_lock = threading.RLock()

        self._sess = self._build_session(retries=self._retries, backoff_s=self._backoff_s, pool_maxsize=self._pool_maxsize)

        self._channels_cache: Optional[List[Dict]] = None
        self._channels_cache_t: float = 0.0

        # profile -> (pairs, t)
        self._m3u_cache: Dict[str, Tuple[List[Tuple[str, str]], float]] = {}

    def _build_session(self, *, retries: int, backoff_s: float, pool_maxsize: int) -> requests.Session:
        sess = requests.Session()

        # Retry on transient errors + dropped connections.
        # TVHeadend can briefly return 503 during restart/upgrade.
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=float(backoff_s),
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)

        sess.headers.update(
            {
                "User-Agent": "tvh-bridge/1.0",
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
            }
        )
        return sess

    def _get(self, url: str, *, params: Optional[Dict] = None) -> requests.Response:
        """GET with a small amount of self-healing.

        Even with urllib3 Retry enabled, a Session can get stuck with a bad pool
        member after network changes. If we see a RequestException, we rebuild
        the session and try once more.
        """
        with self._session_lock:
            try:
                r = self._sess.get(url, params=params, timeout=self._timeout, auth=self._auth, verify=self._verify_tls)
                r.raise_for_status()
                return r
            except requests.RequestException:
                # Rebuild session and retry once.
                try:
                    self._sess.close()
                except Exception:
                    pass
                self._sess = self._build_session(
                    retries=self._retries,
                    backoff_s=self._backoff_s,
                    pool_maxsize=self._pool_maxsize,
                )
                r = self._sess.get(url, params=params, timeout=self._timeout, auth=self._auth, verify=self._verify_tls)
                r.raise_for_status()
                return r

    def close(self) -> None:
        """Close the underlying HTTP session."""
        with self._session_lock:
            try:
                self._sess.close()
            except Exception:
                pass


    def _post(self, url: str, *, data: Optional[Dict] = None) -> requests.Response:
        """POST with the same self-healing approach as _get."""
        with self._session_lock:
            try:
                r = self._sess.post(url, data=data, timeout=self._timeout, auth=self._auth, verify=self._verify_tls)
                r.raise_for_status()
                return r
            except requests.RequestException:
                try:
                    self._sess.close()
                except Exception:
                    pass
                self._sess = self._build_session(
                    retries=self._retries,
                    backoff_s=self._backoff_s,
                    pool_maxsize=self._pool_maxsize,
                )
                r = self._sess.post(url, data=data, timeout=self._timeout, auth=self._auth, verify=self._verify_tls)
                r.raise_for_status()
                return r

    def _cache_valid(self, t: float) -> bool:
        return (time.time() - t) < self.cache_ttl_s

    def list_channels(self, force_refresh: bool = False) -> List[Dict]:
        """Return list of channels: uuid, name, number, enabled."""
        if not force_refresh and self._channels_cache is not None and self._cache_valid(self._channels_cache_t):
            return list(self._channels_cache)

        url = f"{self.base_url}/api/channel/grid"
        try:
            r = self._get(url, params={"start": 0, "limit": 10000})
            try:
                data = r.json()
            except ValueError as e:
                raise RuntimeError(f"TVH returned non-JSON for channel grid: {e}")
        except Exception:
            # If TVH is briefly unavailable, prefer stale-but-useful data.
            if self._channels_cache is not None and not force_refresh:
                return list(self._channels_cache)
            raise
        entries = data.get("entries", [])
        entries.sort(key=lambda c: (c.get("number") or 999999, c.get("name") or ""))

        chans = [
            {
                "uuid": c.get("uuid"),
                "name": c.get("name"),
                "number": c.get("number"),
                "enabled": bool(c.get("enabled", True)),
            }
            for c in entries
            if c.get("uuid") and c.get("name")
        ]

        self._channels_cache = chans
        self._channels_cache_t = time.time()
        return list(chans)

    def _parse_m3u(self, text: str) -> List[Tuple[str, str]]:
        """Parse M3U into (display_name, url)."""
        out: List[Tuple[str, str]] = []
        last_name: Optional[str] = None

        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("#EXTINF:"):
                last_name = ln.split(",", 1)[1].strip() if "," in ln else None
                continue
            if ln.startswith("#"):
                continue
            if ln.startswith("http://") or ln.startswith("https://"):
                if last_name:
                    out.append((last_name, ln))
                last_name = None

        return out

    def _get_playlist_pairs(self, profile: str, force_refresh: bool = False) -> List[Tuple[str, str]]:
        key = profile or "pass"
        cached = self._m3u_cache.get(key)
        if not force_refresh and cached and self._cache_valid(cached[1]):
            return list(cached[0])

        m3u_url = f"{self.base_url}/playlist/channels.m3u"
        try:
            r = self._get(m3u_url, params={"profile": key})
            pairs = self._parse_m3u(r.text)
        except Exception:
            # Same idea: keep the UI/pipeline working if TVH blips.
            if cached and not force_refresh:
                return list(cached[0])
            raise

        self._m3u_cache[key] = (pairs, time.time())
        return list(pairs)

    def get_stream_url_for_uuid(self, channel_uuid: str, profile: str = "pass", force_refresh: bool = False) -> str:
        """Map channel UUID -> stream URL by matching channel name against channels.m3u."""
        chans = self.list_channels(force_refresh=force_refresh)
        chan = next((c for c in chans if c["uuid"] == channel_uuid), None)
        if not chan:
            # One retry with a forced refresh (useful if TVH changed quickly)
            chans = self.list_channels(force_refresh=True)
            chan = next((c for c in chans if c["uuid"] == channel_uuid), None)
        if not chan:
            raise RuntimeError(f"Channel UUID not found: {channel_uuid}")

        name = chan["name"]
        pairs = self._get_playlist_pairs(profile=profile, force_refresh=force_refresh)

        for disp, url in pairs:
            if disp == name:
                return url
        for disp, url in pairs:
            if disp.lower() == name.lower():
                return url

        # Retry with forced playlist refresh
        pairs = self._get_playlist_pairs(profile=profile, force_refresh=True)
        for disp, url in pairs:
            if disp == name or disp.lower() == name.lower():
                return url

        raise RuntimeError(f"Stream URL not found in playlist for channel '{name}'")

    def _post_jsonish(self, path: str, *, data: Optional[Dict] = None) -> Dict:
        r = self._post(f"{self.base_url}{path}", data=data)
        if not r.text:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"text": r.text}


    def list_dvb_scanfiles(self, scan_type: str = "dvb-t") -> List[Dict]:
        """Return Tvheadend's predefined mux/scanfile list for a delivery type.

        This mirrors the Tvheadend web UI "Pre-defined muxes" dropdown. For
        terrestrial setup use scan_type="dvb-t", which covers DVB-T and DVB-T2
        scan tables where present.
        """
        r = self._get(f"{self.base_url}/api/dvb/scanfile/list", params={"type": scan_type})
        data = r.json()
        entries = data.get("entries", [])
        out = []
        for e in entries:
            key = str(e.get("key") or "").strip()
            val = str(e.get("val") or key).strip()
            if key:
                out.append({"key": key, "val": val})
        if str(scan_type or "").strip().lower() == "dvb-t":
            out.append({"key": TELETOOL_UK_AUTO_SCANFILE, "val": TELETOOL_UK_AUTO_LABEL})
        out.sort(key=lambda e: (e.get("val") or "").lower())
        return out

    def _scanfile_roots(self) -> List[Path]:
        roots = [
            Path("/usr/share/tvheadend/data/dvb-scan"),
            Path("/usr/local/share/tvheadend/data/dvb-scan"),
            Path("/usr/share/dvb"),
            Path("/usr/local/share/dvb"),
        ]
        env_root = os.environ.get("TVH_DVB_SCAN_ROOT")
        if env_root:
            roots.insert(0, Path(env_root))
        return roots

    def _scanfile_name_variants(self, scanfile_key: str) -> List[str]:
        """Return likely local filenames for a Tvheadend scanfile key.

        Tvheadend scanfile ids vary by build. Examples seen in the wild include
        ``dvb-t/de/dvb-t_de-All``, ``dvb-t/de/de-All`` and local files named
        ``All`` under ``dvb-t/de``. This also tolerates the Tvheadend API/browser
        occasionally returning a truncated ``...-Al`` key by trying an ``l``
        suffix as a fallback.
        """
        key = str(scanfile_key or "").strip().strip("/")
        parts = [p for p in key.split("/") if p]
        variants: List[str] = []

        def add(v: str) -> None:
            v = str(v or "").strip().strip("/")
            if v and v not in variants:
                variants.append(v)
                if v.endswith("-Al") and (v + "l") not in variants:
                    variants.append(v + "l")

        if parts:
            add(parts[-1])
        if len(parts) >= 3:
            delivery, country, base = parts[0], parts[1], parts[-1]
            if base.startswith(delivery + "_"):
                add(base.split("_", 1)[1])
            if base.startswith(country + "-"):
                add(base)
            # Common local scanfile layouts use just "All" in dvb-t/de/All.
            for prefix in (delivery + "_" + country + "-", country + "-"):
                if base.startswith(prefix):
                    add(base[len(prefix):])
            # If the key is truncated to ...-Al, the local filename is usually All.
            if base.endswith("-Al"):
                add("All")
        return variants

    def _scanfile_candidate_paths(self, scanfile_key: str) -> List[Path]:
        """Best-effort local paths for a Tvheadend scanfile key.

        Tvheadend exposes keys like "dvb-t/de/dvb-t_de-All" but does not expose
        scanfile contents over the JSON API. The scan files are normally present
        under one of these data directories; parsing them lets us create muxes in
        the existing, tuner-attached network rather than creating a detached new
        network.
        """
        key = str(scanfile_key or "").strip().strip("/")
        parts = [p for p in key.split("/") if p]
        names = self._scanfile_name_variants(key)
        candidates: List[Path] = []
        for root in self._scanfile_roots():
            for name in names:
                if len(parts) >= 2:
                    candidates.append(root / parts[0] / parts[1] / name)
                    candidates.append(root / parts[0] / name)
                candidates.append(root / name)
        return list(dict.fromkeys(candidates))

    def _find_scanfile_path(self, scanfile_key: str) -> Path:
        candidates = self._scanfile_candidate_paths(scanfile_key)
        for path in candidates:
            if path.exists() and path.is_file():
                return path

        # Last-resort fuzzy lookup: if Tvheadend gave us a shortened key such as
        # dvb-t/de/dvb-t_de-Al, search the country folder for a file that starts
        # with one of the candidate stems. Keep this deliberately narrow so a
        # wrong country/region is not selected accidentally.
        key = str(scanfile_key or "").strip().strip("/")
        parts = [p for p in key.split("/") if p]
        names = self._scanfile_name_variants(key)
        if len(parts) >= 2:
            for root in self._scanfile_roots():
                for base_dir in (root / parts[0] / parts[1], root / parts[0]):
                    if not base_dir.exists() or not base_dir.is_dir():
                        continue
                    for name in names:
                        prefix = name[:-1] if name.endswith("l") else name
                        if not prefix:
                            continue
                        matches = sorted([p for p in base_dir.glob(prefix + "*") if p.is_file()])
                        if len(matches) == 1:
                            return matches[0]
                        exact_all = base_dir / "All"
                        if name in ("Al", "All") and exact_all.exists() and exact_all.is_file():
                            return exact_all

        tried = ", ".join(str(p) for p in candidates[:16])
        raise RuntimeError(f"Could not find local Tvheadend scanfile for '{scanfile_key}'. Tried: {tried}")

    @staticmethod
    def _clean_scan_value(value: Any) -> str:
        return str(value or "").strip().upper().replace(" ", "")

    @staticmethod
    def _bandwidth_to_tvh(value: Any) -> str:
        s = str(value or "").strip().upper()
        if not s:
            return "AUTO"
        try:
            n = int(s)
            if n >= 1000000:
                return f"{int(n/1000000)}MHz"
            if n >= 1000:
                mhz = n / 1000.0
                return f"{mhz:g}MHz"
        except Exception:
            pass
        s = s.replace("HZ", "").replace(" ", "")
        if s.isdigit():
            return f"{int(s)}MHz"
        return s

    @staticmethod
    def _modulation_to_tvh(value: Any) -> str:
        s = str(value or "").strip().upper().replace(" ", "")
        if s in ("QAM16", "QAM_16"):
            return "QAM/16"
        if s in ("QAM32", "QAM_32"):
            return "QAM/32"
        if s in ("QAM64", "QAM_64"):
            return "QAM/64"
        if s in ("QAM128", "QAM_128"):
            return "QAM/128"
        if s in ("QAM256", "QAM_256"):
            return "QAM/256"
        return s or "AUTO"

    @staticmethod
    def _mode_to_tvh(value: Any) -> str:
        s = str(value or "").strip().upper().replace(" ", "")
        if s.endswith("K") and s[:-1].isdigit():
            return s[:-1] + "k"
        return s or "AUTO"

    @staticmethod
    def _delivery_to_tvh(value: Any) -> str:
        s = str(value or "").strip().upper().replace("-", "").replace("_", "")
        if s in ("DVBT2", "DVB2T"):
            return "DVB-T2"
        return "DVB-T"

    def _parse_dvbv5_scanfile(self, text: str) -> List[Dict[str, Any]]:
        muxes: List[Dict[str, Any]] = []
        current: Dict[str, str] = {}

        def flush() -> None:
            nonlocal current
            if not current:
                return
            freq = current.get("FREQUENCY") or current.get("FREQUENCY_HZ")
            delivery = current.get("DELIVERY_SYSTEM") or current.get("SYSTEM") or "DVBT"
            try:
                freq_i = int(str(freq).strip()) if freq is not None else 0
            except Exception:
                freq_i = 0
            if freq_i > 0:
                conf: Dict[str, Any] = {
                    "enabled": 1,
                    "epg": 1,
                    "delsys": self._delivery_to_tvh(delivery),
                    "frequency": freq_i,
                    "bandwidth": self._bandwidth_to_tvh(current.get("BANDWIDTH_HZ") or current.get("BANDWIDTH")),
                    "fec_hi": self._clean_scan_value(current.get("CODE_RATE_HP") or current.get("CODE_RATE") or "AUTO"),
                    "fec_lo": self._clean_scan_value(current.get("CODE_RATE_LP") or "NONE"),
                    "constellation": self._modulation_to_tvh(current.get("MODULATION") or "AUTO"),
                    "transmission_mode": self._mode_to_tvh(current.get("TRANSMISSION_MODE") or "AUTO"),
                    "guard_interval": self._clean_scan_value(current.get("GUARD_INTERVAL") or "AUTO"),
                    "hierarchy": self._clean_scan_value(current.get("HIERARCHY") or "NONE"),
                    "scan_state": 0,
                    "tsid_zero": False,
                    "pmt_06_ac3": 0,
                    "eit_tsid_nocheck": False,
                    "sid_filter": 0,
                    "charset": "AUTO",
                }
                stream_id = current.get("STREAM_ID") or current.get("PLP_ID")
                try:
                    conf["plp_id"] = int(stream_id) if stream_id not in (None, "", "AUTO") else -1
                except Exception:
                    conf["plp_id"] = -1
                muxes.append(conf)
            current = {}

        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                flush()
                current = {}
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                current[k.strip().upper()] = v.strip()
        flush()
        return muxes

    def _parse_legacy_dvbt_scanfile(self, text: str) -> List[Dict[str, Any]]:
        muxes: List[Dict[str, Any]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            parts = line.split()
            if not parts:
                continue
            prefix = parts.pop(0).upper()
            if prefix not in ("T", "T2"):
                continue
            # Legacy TVH lines: T <freq> <bw> <fec_hi> <fec_lo> <qam> <mode> <guard> <hier> [plp]
            if len(parts) < 8:
                continue
            try:
                freq_i = int(parts[0])
            except Exception:
                continue
            muxes.append({
                "enabled": 1,
                "epg": 1,
                "delsys": "DVB-T2" if prefix == "T2" else "DVB-T",
                "frequency": freq_i,
                "bandwidth": self._bandwidth_to_tvh(parts[1]),
                "fec_hi": self._clean_scan_value(parts[2]),
                "fec_lo": self._clean_scan_value(parts[3]),
                "constellation": self._modulation_to_tvh(parts[4]),
                "transmission_mode": self._mode_to_tvh(parts[5]),
                "guard_interval": self._clean_scan_value(parts[6]),
                "hierarchy": self._clean_scan_value(parts[7]),
                "plp_id": int(parts[8]) if len(parts) > 8 and parts[8].lstrip("-").isdigit() else -1,
                "scan_state": 0,
                "tsid_zero": False,
                "pmt_06_ac3": 0,
                "eit_tsid_nocheck": False,
                "sid_filter": 0,
                "charset": "AUTO",
            })
        return muxes

    def load_scanfile_muxes(self, scanfile_key: str) -> List[Dict[str, Any]]:
        if str(scanfile_key or "").strip() == TELETOOL_UK_AUTO_SCANFILE:
            return self._load_uk_auto_dvbt2_muxes()

        path = self._find_scanfile_path(scanfile_key)
        text = path.read_text(errors="replace")
        muxes = self._parse_dvbv5_scanfile(text)
        if not muxes:
            muxes = self._parse_legacy_dvbt_scanfile(text)
        if not muxes:
            raise RuntimeError(f"No DVB-T/T2 muxes could be parsed from scanfile '{scanfile_key}' at {path}")
        return muxes

    def _load_uk_auto_dvbt2_muxes(self) -> List[Dict[str, Any]]:
        """Build a UK-wide DVB-T/T2 scan set from Tvheadend's transmitter files.

        Tvheadend's built-in ``auto-Default`` table is DVB-T only, so it misses
        UK HD services carried on DVB-T2 multiplexes. This synthetic option keeps
        setup easy while preserving exact UK offsets, PLP ids, and delivery
        systems from the bundled transmitter tables.
        """
        muxes_by_key: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for region in self.list_dvb_scanfiles("dvb-t"):
            key = str(region.get("key") or "")
            if not key.startswith("dvb-t/uk/"):
                continue
            try:
                muxes = self.load_scanfile_muxes(key)
            except Exception:
                continue
            for mux in muxes:
                frequency = mux.get("frequency")
                if not frequency:
                    continue
                mux_key = (
                    int(frequency),
                    str(mux.get("delsys") or ""),
                    str(mux.get("bandwidth") or ""),
                    int(mux.get("plp_id") if mux.get("plp_id") is not None else -1),
                )
                muxes_by_key[mux_key] = dict(mux)
        return [
            muxes_by_key[key]
            for key in sorted(muxes_by_key, key=lambda k: (k[0], k[1], k[3]))
        ]

    def delete_muxes(self, uuids: List[str]) -> None:
        if not uuids:
            return
        self._post_jsonish("/api/idnode/delete", data={"uuid": json.dumps(uuids)})
        self._m3u_cache = {}

    def create_mux(self, network_uuid: str, conf: Dict[str, Any]) -> Dict:
        return self._post_jsonish("/api/mpegts/network/mux_create", data={"uuid": network_uuid, "conf": json.dumps(conf)})

    def replace_muxes_from_scanfile(self, network_uuid: str, scanfile_key: str) -> Dict[str, Any]:
        existing = self.list_muxes_for_network(network_uuid)
        self.delete_muxes([m.get("uuid") for m in existing if m.get("uuid")])
        muxes = self.load_scanfile_muxes(scanfile_key)
        created = 0
        errors: List[str] = []
        for conf in muxes:
            try:
                self.create_mux(network_uuid, conf)
                created += 1
            except Exception as e:
                errors.append(f"{conf.get('frequency')}: {e}")
        if errors and created == 0:
            raise RuntimeError("Failed to create muxes from selected scanfile: " + "; ".join(errors[:5]))
        return {"deleted": len(existing), "created": created, "errors": errors[:10], "scanfile": scanfile_key}

    def list_networks(self) -> List[Dict]:
        r = self._get(f"{self.base_url}/api/mpegts/network/grid", params={"start": 0, "limit": 1000})
        data = r.json()
        return data.get("entries", [])

    def list_services(self, *, hidemode: str = "none") -> List[Dict]:
        r = self._get(
            f"{self.base_url}/api/mpegts/service/grid",
            params={"hidemode": hidemode, "start": 0, "limit": 100000},
        )
        data = r.json()
        return data.get("entries", [])

    def list_muxes_for_network(self, network_uuid: str) -> List[Dict]:
        flt = json.dumps([{"field": "network_uuid", "type": "string", "value": network_uuid}])
        r = self._get(
            f"{self.base_url}/api/mpegts/mux/grid",
            params={"start": 0, "limit": 100000, "filter": flt},
        )
        data = r.json()
        return data.get("entries", [])

    def delete_channels(self, uuids: List[str]) -> None:
        if not uuids:
            return
        self._post_jsonish("/api/idnode/delete", data={"uuid": json.dumps(uuids)})
        self._channels_cache = None
        self._channels_cache_t = 0.0

    def delete_services(self, uuids: List[str]) -> None:
        if not uuids:
            return
        self._post_jsonish("/api/idnode/delete", data={"uuid": json.dumps(uuids)})
        self._m3u_cache = {}

    def scan_network(self, network_uuid: str) -> None:
        self._post_jsonish("/api/mpegts/network/scan", data={"uuid": network_uuid})

    def mapper_status(self) -> Dict:
        r = self._get(f"{self.base_url}/api/service/mapper/status")
        return r.json()

    def status_inputs(self) -> List[Dict]:
        r = self._get(f"{self.base_url}/api/status/inputs", params={"start": 0, "limit": 1000})
        data = r.json()
        return data.get("entries", [])

    def status_subscriptions(self) -> List[Dict]:
        r = self._get(f"{self.base_url}/api/status/subscriptions", params={"start": 0, "limit": 1000})
        data = r.json()
        return data.get("entries", [])

    def map_services(self, service_uuids: List[str]) -> Dict:
        node = {
            "services": service_uuids,
            "encrypted": True,
            "merge_same_name": False,
            "check_availability": False,
            "type_tags": True,
            "provider_tags": False,
            "network_tags": False,
        }
        out = self._post_jsonish("/api/service/mapper/save", data={"node": json.dumps(node)})
        self._channels_cache = None
        self._channels_cache_t = 0.0
        self._m3u_cache = {}
        return out
