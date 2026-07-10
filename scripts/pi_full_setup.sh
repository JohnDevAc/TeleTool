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
#   TELETOOL_NDI_LIB=/home/admin/libndi.so.6

REPO_URL="${TELETOOL_REPO_URL:-https://github.com/JohnDevAc/teletwat.git}"
BRANCH="${TELETOOL_BRANCH:-dev}"
INSTALL_TVHEADEND="${TELETOOL_INSTALL_TVHEADEND:-1}"
TVH_LOCAL_ACCESS="${TELETOOL_TVH_LOCAL_ACCESS:-1}"
APT_UPGRADE="${TELETOOL_APT_UPGRADE:-0}"
NDI_LIB_OVERRIDE="${TELETOOL_NDI_LIB:-}"
GST_NDI_VERSION="${TELETOOL_GST_NDI_VERSION:-0.13.5}"
GST_NDI_SHA256="${TELETOOL_GST_NDI_SHA256:-ec8417e75002857f4c8e8fd2f2f1a7521937eaac3de264f7bb6904a0d22cba23}"
NDI_SDK_URL="https://ndi.video/for-developers/ndi-sdk/"
LDCONFIG="${TELETOOL_LDCONFIG:-/sbin/ldconfig}"
NDI_HELPER_PATH="/usr/local/sbin/teletool-install-ndi-runtime"
NDI_VERIFICATION_MARKER="/var/lib/teletool/ndi-runtime-verified"
TVH_NETWORK_UUID="${TELETOOL_TVH_NETWORK_UUID:-54e1e700000000000000000000000010}"
TVH_NETWORK_NAME="${TELETOOL_TVH_NETWORK_NAME:-DVB-T Network}"
TVH_PROVIDER_NETWORK_NAME="${TELETOOL_TVH_PROVIDER_NETWORK_NAME:-London}"
DVBT_SCANFILE="${TELETOOL_DVBT_SCANFILE:-dvb-t/uk/dvb-t_uk-CrystalPalac}"
export TELETOOL_DVBT_SCANFILE="$DVBT_SCANFILE"

TOTAL_STAGES=8
CURRENT_STAGE_NUMBER=0
CURRENT_STAGE_LABEL="Initial checks"
SETUP_COMPLETE=0
TERMINAL_UI=0

case "${TELETOOL_TERMINAL_UI:-auto}" in
  1|true|yes|on)
    TERMINAL_UI=1
    ;;
  0|false|no|off)
    TERMINAL_UI=0
    ;;
  auto)
    if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
      TERMINAL_UI=1
    fi
    ;;
  *)
    printf 'WARNING: TELETOOL_TERMINAL_UI must be auto, 1, or 0; using auto.\n' >&2
    if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
      TERMINAL_UI=1
    fi
    ;;
esac

C_RESET=""
C_BOLD=""
C_BLUE=""
C_GREEN=""
C_YELLOW=""
C_RED=""
if [ "$TERMINAL_UI" = "1" ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_BLUE=$'\033[38;5;75m'
  C_GREEN=$'\033[38;5;78m'
  C_YELLOW=$'\033[38;5;220m'
  C_RED=$'\033[38;5;203m'
fi

terminal_clear() {
  if [ "$TERMINAL_UI" = "1" ]; then
    printf '\033[2J\033[H'
  fi
}

terminal_title() {
  if [ "$TERMINAL_UI" = "1" ]; then
    printf '\033]0;TeleTool Setup - %s\007' "$1"
  fi
}

terminal_reset() {
  if [ "$TERMINAL_UI" = "1" ]; then
    printf '%s\033[?25h' "$C_RESET"
  fi
}

progress_bar() {
  local current="$1" total="$2" width=34 filled empty filled_bar empty_bar
  filled=$((current * width / total))
  empty=$((width - filled))
  printf -v filled_bar '%*s' "$filled" ''
  printf -v empty_bar '%*s' "$empty" ''
  filled_bar="${filled_bar// /#}"
  empty_bar="${empty_bar// /-}"
  printf '[%s%s]' "$filled_bar" "$empty_bar"
}

begin_stage() {
  CURRENT_STAGE_NUMBER="$1"
  CURRENT_STAGE_LABEL="$2"

  if [ "$TERMINAL_UI" != "1" ]; then
    printf '\n==> [%s/%s] %s\n' "$CURRENT_STAGE_NUMBER" "$TOTAL_STAGES" "$CURRENT_STAGE_LABEL"
    return
  fi

  terminal_clear
  terminal_title "$CURRENT_STAGE_LABEL"
  printf '%s%s  TELETOOL RASPBERRY PI SETUP%s\n' "$C_BLUE" "$C_BOLD" "$C_RESET"
  printf '  ==============================================================================\n'
  printf '  Stage %s of %s  ' "$CURRENT_STAGE_NUMBER" "$TOTAL_STAGES"
  progress_bar "$CURRENT_STAGE_NUMBER" "$TOTAL_STAGES"
  printf '\n'
  printf '  %s%s%s\n' "$C_BOLD" "$CURRENT_STAGE_LABEL" "$C_RESET"
  printf '  ==============================================================================\n\n'
}

status_line() {
  local state="$1" message="$2" colour="$C_BLUE"
  case "$state" in
    OK|READY) colour="$C_GREEN" ;;
    ACTION|WARN) colour="$C_YELLOW" ;;
    ERROR) colour="$C_RED" ;;
  esac
  printf '  %s[%s]%s %s\n' "$colour" "$state" "$C_RESET" "$message"
}

on_exit() {
  local exit_code=$?
  terminal_reset
  if [ "$exit_code" -ne 0 ] && [ "$SETUP_COMPLETE" != "1" ]; then
    printf '\n%s%s  SETUP STOPPED%s\n' "$C_RED" "$C_BOLD" "$C_RESET" >&2
    printf '  Stage %s of %s: %s\n' "$CURRENT_STAGE_NUMBER" "$TOTAL_STAGES" "$CURRENT_STAGE_LABEL" >&2
    printf '  Fix the error shown above and run the installer again.\n' >&2
  fi
}
trap on_exit EXIT

log() {
  printf '\n%s%s==>%s %s\n' "$C_BLUE" "$C_BOLD" "$C_RESET" "$*"
}

warn() {
  printf '%sWARNING:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

die() {
  printf '%sERROR:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
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
NDI_LIB_SOURCE="${NDI_LIB_OVERRIDE:-$SERVICE_HOME/libndi.so.6}"

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

ndi_runtime_available() {
  "$LDCONFIG" -p 2>/dev/null | awk '
    $1 == "libndi.so.6" { found = 1 }
    END { exit !found }
  '
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
    build-essential \
    cargo \
    curl \
    git \
    jq \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    pkg-config \
    rsync \
    rustc \
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

install_gstreamer_ndi_plugin() {
  local plugin_dir plugin_file build_dir archive source_dir

  plugin_dir="$(pkg-config --variable=pluginsdir gstreamer-1.0)"
  [ -n "$plugin_dir" ] || die "Could not determine the GStreamer plugin directory."
  plugin_file="$plugin_dir/libgstndi.so"

  if [ -f "$plugin_file" ]; then
    log "GStreamer NDI plugin is already installed"
    return
  fi

  log "Building GStreamer NDI plugin $GST_NDI_VERSION"
  build_dir="$(mktemp -d)"
  archive="$build_dir/gst-plugin-ndi-$GST_NDI_VERSION.crate"
  source_dir="$build_dir/gst-plugin-ndi-$GST_NDI_VERSION"

  curl -A "TeleTool pi_full_setup" -fL \
    "https://static.crates.io/crates/gst-plugin-ndi/gst-plugin-ndi-$GST_NDI_VERSION.crate" \
    -o "$archive"
  printf '%s  %s\n' "$GST_NDI_SHA256" "$archive" | sha256sum -c -
  tar -xzf "$archive" -C "$build_dir"
  [ -f "$source_dir/Cargo.toml" ] || die "The GStreamer NDI source archive was not extracted as expected."

  run_root chown -R "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$build_dir"
  run_as_service_user env \
    HOME="$SERVICE_HOME" \
    CARGO_HOME="$SERVICE_HOME/.cargo" \
    cargo build --locked --release --manifest-path "$source_dir/Cargo.toml"

  [ -f "$source_dir/target/release/libgstndi.so" ] || die "GStreamer NDI plugin build did not produce libgstndi.so."
  run_root install -d -m 0755 "$plugin_dir"
  run_root install -m 0644 "$source_dir/target/release/libgstndi.so" "$plugin_file"
  run_root "$LDCONFIG"
  rm -rf "$build_dir"
}

install_ndi_runtime() {
  if ndi_runtime_available && [ -f "$NDI_VERIFICATION_MARKER" ]; then
    log "NDI SDK 6 runtime is already installed and verified"
    return
  fi

  if [ ! -f "$NDI_LIB_SOURCE" ] && ! ndi_runtime_available; then
    warn "NDI SDK runtime is not present at $NDI_LIB_SOURCE"
    return
  fi

  [ -x "$NDI_HELPER_PATH" ] || die "Missing verified NDI runtime installer: $NDI_HELPER_PATH"
  log "Validating and installing user-supplied NDI SDK 6 runtime"
  run_root "$NDI_HELPER_PATH"

  if [ -d "$SERVICE_HOME/.cache/gstreamer-1.0" ]; then
    run_as_service_user find "$SERVICE_HOME/.cache/gstreamer-1.0" \
      -maxdepth 1 -type f -name 'registry.*.bin' -delete
  fi
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
  env \
    TELETOOL_SERVICE_USER="$SERVICE_USER" \
    TELETOOL_NDI_LIB="$NDI_LIB_SOURCE" \
    bash "$PROJECT_DIR/scripts/pi_setup.sh"
}

print_summary() {
  local ip host web_host teletool_url tvheadend_url
  local teletool_ok=0 tvheadend_ok=0 ndi_plugin_ok=0 ndi_runtime_ok=0 alsa_ok=0

  ip="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+\./ && $i !~ /^127\./) {print $i; exit}}')"
  host="$(hostname -s 2>/dev/null || printf 'teletool')"
  if [ -n "$ip" ]; then
    web_host="$ip"
  else
    web_host="$host.local"
  fi
  teletool_url="http://$web_host:8000/"
  tvheadend_url="http://$web_host:9981/"

  systemctl is-active --quiet "$SERVICE_NAME" && teletool_ok=1
  if [ "$INSTALL_TVHEADEND" = "1" ]; then
    systemctl is-active --quiet tvheadend && tvheadend_ok=1
  fi
  if gst-inspect-1.0 ndisink >/dev/null 2>&1 && \
     gst-inspect-1.0 ndisinkcombiner >/dev/null 2>&1; then
    ndi_plugin_ok=1
  fi
  if ndi_runtime_available && [ -f "$NDI_VERIFICATION_MARKER" ]; then
    ndi_runtime_ok=1
  fi
  gst-inspect-1.0 alsasink >/dev/null 2>&1 && alsa_ok=1

  if [ "$TERMINAL_UI" = "1" ]; then
    terminal_clear
    terminal_title "Complete"
  else
    printf '\n'
  fi

  printf '%s%s  TELETOOL INSTALLATION COMPLETE%s\n' "$C_GREEN" "$C_BOLD" "$C_RESET"
  printf '  ==============================================================================\n\n'

  [ "$teletool_ok" = "1" ] && \
    status_line OK "TeleTool service is running" || \
    status_line ERROR "TeleTool service is not running; check systemctl status $SERVICE_NAME"
  if [ "$INSTALL_TVHEADEND" = "1" ]; then
    [ "$tvheadend_ok" = "1" ] && \
      status_line OK "Tvheadend service is running" || \
      status_line WARN "Tvheadend is not running; check systemctl status tvheadend"
  fi
  [ "$ndi_plugin_ok" = "1" ] && \
    status_line OK "GStreamer NDI plugin is installed" || \
    status_line ERROR "GStreamer NDI plugin verification failed"
  [ "$alsa_ok" = "1" ] && \
    status_line OK "GStreamer ALSA audio output is installed" || \
    status_line WARN "GStreamer ALSA audio output was not detected"
  if [ "$ndi_runtime_ok" = "1" ]; then
    status_line READY "NDI SDK runtime is installed and verified"
  else
    status_line ACTION "NDI SDK runtime must be uploaded in the TeleTool Web UI"
  fi

  printf '\n  %s%sOPEN THE UNIT WEB UI%s\n' "$C_BLUE" "$C_BOLD" "$C_RESET"
  printf '  ------------------------------------------------------------------------------\n'
  printf '  %s%s%s\n' "$C_BOLD" "$teletool_url" "$C_RESET"
  printf '  ------------------------------------------------------------------------------\n'

  if [ "$ndi_runtime_ok" != "1" ]; then
    printf '\n  %sFinish NDI setup in the browser:%s\n' "$C_BOLD" "$C_RESET"
    printf '  1. Open the TeleTool link above.\n'
    printf '  2. Download the NDI SDK for Linux from:\n'
    printf '     %s\n' "$NDI_SDK_URL"
    printf '  3. Extract the ARM64 file named libndi.so.6.\n'
    printf '  4. Drop it onto the upload box. TeleTool will install and verify it.\n'
    printf '  5. When verification completes, TeleTool opens normally.\n'
  else
    printf '\n  TeleTool is ready to use.\n'
  fi

  printf '\n  Next: choose the DVB-T/T2 transmitter in TV Setup and run the scan.\n'
  if [ "$INSTALL_TVHEADEND" = "1" ]; then
    printf '  Tvheadend UI: %s\n' "$tvheadend_url"
  fi
  printf '\n  This completion screen remains in the terminal for reference.\n\n'
}

main() {
  if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
    die "sudo is required when not running as root."
  fi

  begin_stage 1 "Installing Raspberry Pi OS packages"
  printf '  Service user:  %s\n' "$SERVICE_USER"
  printf '  Project dir:   %s\n' "$PROJECT_DIR"
  printf '  Branch:        %s\n' "$BRANCH"
  printf '  DVB-T region:  %s\n' "$DVBT_SCANFILE"
  printf '  NDI drop path: %s\n' "$NDI_LIB_SOURCE"
  printf '\n'
  install_base_packages

  begin_stage 2 "Installing the GStreamer NDI plugin"
  install_gstreamer_ndi_plugin

  begin_stage 3 "Preparing the TeleTool application"
  ensure_project_checkout
  write_release_marker

  begin_stage 4 "Preparing the TeleTool configuration"
  ensure_config_json

  begin_stage 5 "Configuring Tvheadend"
  configure_tvheadend

  begin_stage 6 "Installing and starting TeleTool"
  install_teletool_service

  begin_stage 7 "Checking the NDI SDK runtime"
  install_ndi_runtime

  begin_stage 8 "Running final checks"
  print_summary
  SETUP_COMPLETE=1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
