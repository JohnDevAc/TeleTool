#!/usr/bin/env bash
set -euo pipefail

# Full clean-install bootstrap for Raspberry Pi OS Lite.
#
# Typical use on a fresh Pi:
#   curl -fsSL https://raw.githubusercontent.com/JohnDevAc/teletwat/dev/scripts/pi_full_setup.sh | bash
#
# Useful overrides:
#   TELETOOL_BRANCH=dev
#   TELETOOL_PROJECT_DIR=/home/admin/tvh_ndi_bridge
#   TELETOOL_SERVICE_USER=admin
#   TELETOOL_DVBT_SCANFILE=dvb-t/uk/dvb-t_uk-CrystalPalac
#   TELETOOL_NDI_DEB=/home/admin/path/to/ndi-plugin.deb

REPO_URL="${TELETOOL_REPO_URL:-https://github.com/JohnDevAc/teletwat.git}"
BRANCH="${TELETOOL_BRANCH:-dev}"
INSTALL_TVHEADEND="${TELETOOL_INSTALL_TVHEADEND:-1}"
TVH_LOCAL_ACCESS="${TELETOOL_TVH_LOCAL_ACCESS:-1}"
APT_UPGRADE="${TELETOOL_APT_UPGRADE:-0}"
NDI_DEB="${TELETOOL_NDI_DEB:-}"
TVH_NETWORK_UUID="${TELETOOL_TVH_NETWORK_UUID:-54e1e700000000000000000000000010}"
TVH_NETWORK_NAME="${TELETOOL_TVH_NETWORK_NAME:-DVB-T Network}"
TVH_PROVIDER_NETWORK_NAME="${TELETOOL_TVH_PROVIDER_NETWORK_NAME:-London}"
DVBT_SCANFILE="${TELETOOL_DVBT_SCANFILE:-dvb-t/uk/dvb-t_uk-CrystalPalac}"
export TELETOOL_DVBT_SCANFILE="$DVBT_SCANFILE"

log() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

detect_service_user() {
  if [ -n "${TELETOOL_SERVICE_USER:-}" ]; then
    printf '%s\n' "$TELETOOL_SERVICE_USER"
    return
  fi
  if [ "$(id -u)" -ne 0 ]; then
    id -un
    return
  fi
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    printf '%s\n' "$SUDO_USER"
    return
  fi
  awk -F: '$3 >= 1000 && $3 < 60000 { print $1; exit }' /etc/passwd
}

SERVICE_USER="$(detect_service_user)"
[ -n "$SERVICE_USER" ] || die "Could not determine service user. Set TELETOOL_SERVICE_USER."
getent passwd "$SERVICE_USER" >/dev/null || die "Service user '$SERVICE_USER' does not exist."
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
PROJECT_DIR="${TELETOOL_PROJECT_DIR:-$SERVICE_HOME/tvh_ndi_bridge}"
SERVICE_NAME="tvh_ndi_bridge.service"

run_as_service_user() {
  if [ "$(id -un)" = "$SERVICE_USER" ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$SERVICE_USER" "$@"
  else
    runuser -u "$SERVICE_USER" -- "$@"
  fi
}

apt_install() {
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

apt_install_optional() {
  local pkg
  for pkg in "$@"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      apt_install "$pkg"
    else
      warn "Optional package '$pkg' is not available in the current apt sources."
    fi
  done
}

detect_project_dir_from_script() {
  local script_dir candidate
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
  candidate="$(cd "$script_dir/.." 2>/dev/null && pwd || true)"
  if [ -n "$candidate" ] && [ -f "$candidate/app.py" ] && [ -f "$candidate/scripts/pi_setup.sh" ]; then
    printf '%s\n' "$candidate"
  fi
}

install_base_packages() {
  log "Installing Raspberry Pi OS packages"
  run_root apt-get update
  if [ "$APT_UPGRADE" = "1" ]; then
    run_root env DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y
  fi

  apt_install \
    ca-certificates \
    curl \
    git \
    jq \
    rsync \
    sudo \
    tar \
    unzip \
    python3-venv \
    python3-pip \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    alsa-utils \
    avahi-daemon \
    network-manager \
    gstreamer1.0-tools \
    gstreamer1.0-alsa \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav

  apt_install_optional \
    dtv-scan-tables \
    dvb-tools \
    v4l-utils

  if [ "$INSTALL_TVHEADEND" = "1" ]; then
    apt_install tvheadend
  fi

  run_root systemctl enable --now avahi-daemon 2>/dev/null || true
  run_root systemctl enable --now NetworkManager 2>/dev/null || true
}

install_optional_ndi_package() {
  if [ -z "$NDI_DEB" ]; then
    return
  fi
  [ -f "$NDI_DEB" ] || die "TELETOOL_NDI_DEB points to a missing file: $NDI_DEB"
  log "Installing supplied NDI package"
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$NDI_DEB"
}

ensure_project_checkout() {
  local embedded_project
  embedded_project="$(detect_project_dir_from_script || true)"
  if [ -n "$embedded_project" ] && [ "$PROJECT_DIR" = "$embedded_project" ]; then
    log "Using local TeleTool checkout at $PROJECT_DIR"
    return
  fi

  if [ -f "$PROJECT_DIR/app.py" ] && [ -f "$PROJECT_DIR/scripts/pi_setup.sh" ]; then
    log "Using existing TeleTool checkout at $PROJECT_DIR"
  else
    log "Cloning TeleTool $BRANCH branch to $PROJECT_DIR"
    run_root install -d -o "$SERVICE_USER" -g "$(id -gn "$SERVICE_USER")" "$PROJECT_DIR"
    run_as_service_user git clone --branch "$BRANCH" "$REPO_URL" "$PROJECT_DIR"
  fi

  if [ -d "$PROJECT_DIR/.git" ]; then
    log "Updating checkout to $BRANCH"
    run_as_service_user git -C "$PROJECT_DIR" fetch origin "$BRANCH"
    run_as_service_user git -C "$PROJECT_DIR" checkout "$BRANCH"
    run_as_service_user git -C "$PROJECT_DIR" pull --ff-only origin "$BRANCH"
  fi
}

write_release_marker() {
  local label="Main" development="false"
  if [ "$BRANCH" = "dev" ]; then
    label="Dev"
    development="true"
  fi
  cat > "$PROJECT_DIR/.teletool_release.json" <<JSON
{
  "branch": "$BRANCH",
  "label": "$label",
  "development": $development
}
JSON
  run_root chown "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$PROJECT_DIR/.teletool_release.json" 2>/dev/null || true
}

ensure_config_json() {
  log "Preparing TeleTool runtime config"
  if [ ! -f "$PROJECT_DIR/config.json" ] && [ -f "$PROJECT_DIR/config.example.json" ]; then
    run_as_service_user cp "$PROJECT_DIR/config.example.json" "$PROJECT_DIR/config.json"
  fi

  run_as_service_user env \
    TELETOOL_TVH_BASE_URL="${TELETOOL_TVH_BASE_URL:-http://127.0.0.1:9981}" \
    TELETOOL_NDI_NAME="${TELETOOL_NDI_NAME:-TeleTool}" \
    TELETOOL_DVBT_SCANFILE="$DVBT_SCANFILE" \
    TELETOOL_TVH_NETWORK_UUID="$TVH_NETWORK_UUID" \
    TELETOOL_TVH_NETWORK_NAME="$TVH_NETWORK_NAME" \
    python3 - "$PROJECT_DIR/config.json" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = {}
if path.exists():
    data = json.loads(path.read_text() or "{}")

data.setdefault("tvh_base_url", os.environ.get("TELETOOL_TVH_BASE_URL", "http://127.0.0.1:9981"))
data.setdefault("tvh_stream_profile", "pass")
data.setdefault("ndi_default_name", os.environ.get("TELETOOL_NDI_NAME", "TeleTool"))
data.setdefault("tvh_dvbt_network_uuid", os.environ["TELETOOL_TVH_NETWORK_UUID"])
data.setdefault("tvh_dvbt_network_name", os.environ["TELETOOL_TVH_NETWORK_NAME"])

scanfile = os.environ.get("TELETOOL_DVBT_SCANFILE")
if scanfile:
    data["tvh_dvbt_scanfile"] = scanfile

path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

configure_tvheadend() {
  if [ "$INSTALL_TVHEADEND" != "1" ] || [ "$TVH_LOCAL_ACCESS" != "1" ]; then
    return
  fi

  log "Configuring Tvheadend auth and DVB-T network"
  run_root systemctl enable --now tvheadend 2>/dev/null || true
  sleep 2

  local tvh_dir=""
  for candidate in /home/hts/.hts/tvheadend /var/lib/tvheadend; do
    if [ -d "$candidate" ]; then
      tvh_dir="$candidate"
      break
    fi
  done
  if [ -z "$tvh_dir" ]; then
    tvh_dir="/var/lib/tvheadend"
    run_root mkdir -p "$tvh_dir"
  fi

  local access_prefix="${TELETOOL_TVH_ACCESS_PREFIX:-127.0.0.1/32,::1/128}"
  if [ "${TELETOOL_TVH_OPEN_LAN:-0}" = "1" ]; then
    access_prefix="0.0.0.0/0,::/0"
  fi

  local tvh_user="hts" tvh_group="video"
  if id hts >/dev/null 2>&1; then
    tvh_user="hts"
    tvh_group="$(id -gn hts)"
  fi

  run_root systemctl stop tvheadend 2>/dev/null || true
  run_root mkdir -p "$tvh_dir/accesscontrol" "$tvh_dir/passwd" "$tvh_dir/input/dvb/networks/$TVH_NETWORK_UUID"

  run_root env \
    TVH_DIR="$tvh_dir" \
    TVH_USER="$tvh_user" \
    TVH_GROUP="$tvh_group" \
    TVH_ACCESS_PREFIX="$access_prefix" \
    TVH_NETWORK_UUID="$TVH_NETWORK_UUID" \
    TVH_NETWORK_NAME="$TVH_NETWORK_NAME" \
    TVH_PROVIDER_NETWORK_NAME="$TVH_PROVIDER_NETWORK_NAME" \
    python3 - <<'PY'
import json
import os
import shutil
from pathlib import Path

tvh_dir = Path(os.environ["TVH_DIR"])
tvh_user = os.environ["TVH_USER"]
tvh_group = os.environ["TVH_GROUP"]
prefix = os.environ["TVH_ACCESS_PREFIX"]
network_uuid = os.environ["TVH_NETWORK_UUID"]
network_name = os.environ["TVH_NETWORK_NAME"]
provider_network_name = os.environ["TVH_PROVIDER_NETWORK_NAME"]

def write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(path, mode)
    shutil.chown(path, user=tvh_user, group=tvh_group)

# Blank username and blank password admin credentials. Prefix is local-only by
# default so TeleTool can call Tvheadend without embedding credentials.
write_json(
    tvh_dir / "accesscontrol" / "54e1e700000000000000000000000001",
    {
        "enabled": True,
        "username": "",
        "prefix": prefix,
        "streaming": True,
        "profile": True,
        "dvr": True,
        "dvr_config": True,
        "webui": True,
        "admin": True,
        "conn_limit_type": 0,
        "channel_min": 0,
        "channel_max": 0,
        "channel_tag_exclude": False,
        "comment": "TeleTool blank local admin",
    },
)
write_json(
    tvh_dir / "accesscontrol" / "54e1e700000000000000000000000003",
    {
        "enabled": True,
        "username": "*",
        "prefix": prefix,
        "streaming": True,
        "profile": True,
        "dvr": True,
        "dvr_config": True,
        "webui": True,
        "admin": True,
        "conn_limit_type": 0,
        "channel_min": 0,
        "channel_max": 0,
        "channel_tag_exclude": False,
        "comment": "TeleTool wildcard local admin",
    },
)
write_json(
    tvh_dir / "passwd" / "54e1e700000000000000000000000002",
    {
        "enabled": True,
        "username": "",
        "password": "",
    },
)

write_json(
    tvh_dir / "input" / "dvb" / "networks" / network_uuid / "config",
    {
        "enabled": True,
        "networkname": network_name,
        "pnetworkname": provider_network_name,
        "nid": 0,
        "autodiscovery": 1,
        "bouquet": False,
        "skipinitscan": True,
        "idlescan": False,
        "sid_chnum": False,
        "ignore_chnum": False,
        "satip_source": 0,
        "localtime": 0,
        "wizard": True,
        "class": "dvb_network_dvbt",
    },
)

adapter_dir = tvh_dir / "input" / "linuxdvb" / "adapters"
updated = 0
if adapter_dir.exists():
    for path in adapter_dir.iterdir():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text() or "{}")
        except Exception:
            continue
        frontends = data.get("frontends")
        if not isinstance(frontends, dict):
            continue
        changed = False
        for frontend in frontends.values():
            if not isinstance(frontend, dict):
                continue
            frontend_type = str(frontend.get("type") or "").upper()
            display_name = str(frontend.get("displayname") or "").upper()
            if "DVB-T" not in frontend_type and "DVB-T" not in display_name:
                continue
            networks = frontend.get("networks")
            if not isinstance(networks, list):
                networks = []
            if network_uuid not in networks:
                networks.append(network_uuid)
            frontend["networks"] = networks
            frontend["enabled"] = True
            frontend["ota_epg"] = True
            frontend["initscan"] = True
            frontend["idlescan"] = True
            changed = True
        if changed:
            write_json(path, data)
            updated += 1

print(f"Configured Tvheadend access prefix {prefix}; DVB-T adapter files updated: {updated}")
PY

  run_root chown -R "$tvh_user":"$tvh_group" "$tvh_dir/accesscontrol" "$tvh_dir/passwd" "$tvh_dir/input" 2>/dev/null || true
  run_root systemctl start tvheadend

  if ! curl -fsS --max-time 8 http://127.0.0.1:9981/api/serverinfo >/dev/null 2>&1; then
    warn "Tvheadend local API is not reachable yet. Check: systemctl status tvheadend"
    return
  fi

  if curl -fsS --max-time 8 http://127.0.0.1:9981/api/mpegts/network/grid >/dev/null 2>&1; then
    log "Tvheadend local admin access is working"
  else
    warn "Tvheadend server is up, but local admin API access is not working."
  fi
}

install_teletool_service() {
  log "Installing TeleTool Python environment and systemd service"
  run_root chown -R "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$PROJECT_DIR"
  env TELETOOL_SERVICE_USER="$SERVICE_USER" bash "$PROJECT_DIR/scripts/pi_setup.sh"
}

print_summary() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

  log "Final checks"
  systemctl --no-pager --full status "$SERVICE_NAME" || true
  systemctl --no-pager --full status tvheadend 2>/dev/null || true

  if gst-inspect-1.0 ndisink >/dev/null 2>&1; then
    printf 'NDI GStreamer sink: found\n'
  else
    warn "NDI GStreamer sink was not found. Install an ARM64 NDI runtime/GStreamer plugin that provides ndisink."
  fi

  if gst-inspect-1.0 alsasink >/dev/null 2>&1; then
    printf 'ALSA GStreamer sink: found\n'
  else
    warn "ALSA GStreamer sink was not found."
  fi

  printf '\nTeleTool setup complete.\n'
  if [ -n "$ip" ]; then
    printf 'TeleTool UI:   http://%s:8000/\n' "$ip"
    printf 'Tvheadend UI:  http://%s:9981/\n' "$ip"
  fi
  printf '\nNext step: open TeleTool, choose the correct DVB-T/T2 transmitter in TV Setup, then run the scan.\n'
}

main() {
  if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
    die "sudo is required when not running as root."
  fi

  log "TeleTool full Raspberry Pi OS Lite setup"
  printf 'Service user: %s\n' "$SERVICE_USER"
  printf 'Project dir:  %s\n' "$PROJECT_DIR"
  printf 'Branch:       %s\n' "$BRANCH"
  printf 'DVB-T region: %s\n' "$DVBT_SCANFILE"

  install_base_packages
  install_optional_ndi_package
  ensure_project_checkout
  write_release_marker
  ensure_config_json
  configure_tvheadend
  install_teletool_service
  print_summary
}

main "$@"
