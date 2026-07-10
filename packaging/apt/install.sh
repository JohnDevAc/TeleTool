#!/bin/sh
set -eu

# build_apt_repo.sh prepends TeleTool's terminal UI helpers. Keep plain-text
# fallbacks so this source file is also safe to run directly during development.
if ! command -v tt_ui_init >/dev/null 2>&1; then
  TT_UI_YELLOW=""
  TT_UI_RESET=""
  tt_ui_init() { :; }
  tt_ui_reset() { :; }
  tt_ui_stage() { printf '\n==> [%s/%s] %s\n' "$1" "$2" "$3"; }
  tt_ui_progress() { printf '\n==> [%s%%] %s\n' "$1" "$2"; }
  tt_ui_completion_header() { printf '\nTELETOOL READY\n\n'; }
  tt_ui_web_link() { printf '\nOPEN THIS ADDRESS IN A WEB BROWSER\n\n  %s\n\n' "$1"; }
  tt_ui_failure() { printf '\nINSTALLATION STOPPED: %s\n' "$1" >&2; }
  tt_ui_status() { printf '  [%s] %s\n' "$1" "$2"; }
fi

REPOSITORY_URL="https://johndevac.github.io/teletwat/apt-repo"
KEYRING="/usr/share/keyrings/teletool-archive-keyring.gpg"
SOURCE_FILE="/etc/apt/sources.list.d/teletool.sources"
LOG_FILE="/var/log/teletool-installer.log"

tt_ui_init
trap tt_ui_reset EXIT
tt_ui_progress 2 "Checking Raspberry Pi OS" "Confirming this unit can run TeleTool"

if [ "$(id -u)" -ne 0 ]; then
  tt_ui_failure "Run this installer as root, for example: curl ... | sudo sh"
  exit 1
fi
if [ "$(dpkg --print-architecture)" != "arm64" ]; then
  tt_ui_failure "TeleTool currently supports only 64-bit ARM Raspberry Pi OS."
  exit 1
fi

install -d -m 0755 /var/log
: > "$LOG_FILE"
chmod 0644 "$LOG_FILE"

tt_ui_progress 8 "Preparing the TeleTool package source" "Package output is being recorded in $LOG_FILE"
install -d -m 0755 /usr/share/keyrings
tmp_key="$(mktemp)"
if ! curl -fsSL "$REPOSITORY_URL/teletool-archive-keyring.gpg" -o "$tmp_key" >>"$LOG_FILE" 2>&1; then
  rm -f "$tmp_key"
  tt_ui_failure "Could not download the TeleTool repository signing key." "$LOG_FILE"
  exit 1
fi
install -m 0644 "$tmp_key" "$KEYRING"
rm -f "$tmp_key"

cat > "$SOURCE_FILE" <<EOF
Types: deb
URIs: $REPOSITORY_URL
Suites: stable
Components: main
Architectures: arm64
Signed-By: $KEYRING
EOF

tt_ui_progress 15 "Refreshing package information" "Checking Raspberry Pi OS and TeleTool repositories"
if ! apt-get -qq -o Dpkg::Use-Pty=0 update >>"$LOG_FILE" 2>&1; then
  tt_ui_failure "Package information could not be refreshed." "$LOG_FILE"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
tt_ui_progress 30 "Downloading TeleTool" "Downloading TeleTool, Tvheadend and media dependencies"
if ! apt-get -qq -o Dpkg::Use-Pty=0 --download-only install -y teletool >>"$LOG_FILE" 2>&1; then
  tt_ui_failure "TeleTool packages could not be downloaded." "$LOG_FILE"
  exit 1
fi

tt_ui_progress 60 "Installing TeleTool" "Configuring Tvheadend, GStreamer and the Web UI"
export TELETOOL_DEFER_COMPLETION=1
if ! apt-get -qq -o Dpkg::Use-Pty=0 --no-download install -y teletool >>"$LOG_FILE" 2>&1; then
  tt_ui_failure "TeleTool could not be installed." "$LOG_FILE"
  exit 1
fi
unset TELETOOL_DEFER_COMPLETION

tt_ui_progress 95 "Verifying the installation" "Starting TeleTool and Tvheadend"

ip="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+\./ && $i !~ /^127\./) {print $i; exit}}')"
if [ -z "$ip" ]; then
  ip="$(hostname -s 2>/dev/null || printf teletool).local"
fi

teletool_ready=1
tvheadend_ready=1
systemctl is-active --quiet teletool.service 2>/dev/null || teletool_ready=0
systemctl is-active --quiet tvheadend.service 2>/dev/null || tvheadend_ready=0

tt_ui_progress 100 "Installation complete" "TeleTool is ready"
tt_ui_completion_header
tt_ui_web_link "http://$ip:8000/"
if [ "$teletool_ready" -ne 1 ]; then
  tt_ui_status ERROR "TeleTool service did not start; run systemctl status teletool"
fi
if [ "$tvheadend_ready" -ne 1 ]; then
  tt_ui_status WARN "Tvheadend service is not running"
fi

runtime_ready=0
if ldconfig -p 2>/dev/null | grep -q 'libndi\.so\.6' && \
   [ -f /var/lib/teletool/ndi-runtime-verified ]; then
  runtime_ready=1
fi

if [ "$runtime_ready" -ne 1 ]; then
  printf '\n  %sNDI SDK REQUIRED%s\n' "${TT_UI_YELLOW:-}" "${TT_UI_RESET:-}"
  printf '  Open the address above and upload the ARM64 libndi.so.6 file.\n'
  printf '  SDK: https://ndi.video/for-developers/ndi-sdk/\n'
fi
printf '\n'
