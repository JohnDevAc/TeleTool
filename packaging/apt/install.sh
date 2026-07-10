#!/bin/sh
set -eu

# build_apt_repo.sh prepends TeleTool's terminal UI helpers. Keep plain-text
# fallbacks so this source file is also safe to run directly during development.
if ! command -v tt_ui_init >/dev/null 2>&1; then
  tt_ui_init() { :; }
  tt_ui_reset() { :; }
  tt_ui_stage() { printf '\n==> [%s/%s] %s\n' "$1" "$2" "$3"; }
  tt_ui_completion_header() { printf '\nTELETOOL INSTALLATION COMPLETE\n\n'; }
  tt_ui_status() { printf '  [%s] %s\n' "$1" "$2"; }
fi

REPOSITORY_URL="https://johndevac.github.io/teletwat"
KEYRING="/usr/share/keyrings/teletool-archive-keyring.gpg"
SOURCE_FILE="/etc/apt/sources.list.d/teletool.sources"

tt_ui_init
trap tt_ui_reset EXIT
tt_ui_stage 1 4 "Checking Raspberry Pi OS"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root, for example: curl ... | sudo sh" >&2
  exit 1
fi
if [ "$(dpkg --print-architecture)" != "arm64" ]; then
  echo "TeleTool currently supports only 64-bit ARM Raspberry Pi OS." >&2
  exit 1
fi

tt_ui_status OK "64-bit ARM Raspberry Pi OS detected"
tt_ui_stage 2 4 "Adding the signed TeleTool repository"
install -d -m 0755 /usr/share/keyrings
tmp_key="$(mktemp)"
curl -fsSL "$REPOSITORY_URL/teletool-archive-keyring.gpg" -o "$tmp_key"
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

tt_ui_status OK "TeleTool package source installed"
tt_ui_stage 3 4 "Refreshing package information"
apt-get update

tt_ui_stage 4 4 "Installing TeleTool and its dependencies"
DEBIAN_FRONTEND=noninteractive apt-get install -y teletool

ip="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+\./ && $i !~ /^127\./) {print $i; exit}}')"
if [ -z "$ip" ]; then
  ip="$(hostname -s 2>/dev/null || printf teletool).local"
fi

tt_ui_completion_header
if systemctl is-active --quiet teletool.service 2>/dev/null; then
  tt_ui_status OK "TeleTool service is running"
else
  tt_ui_status ERROR "TeleTool service did not start; run systemctl status teletool"
fi
if systemctl is-active --quiet tvheadend.service 2>/dev/null; then
  tt_ui_status OK "Tvheadend service is running"
else
  tt_ui_status WARN "Tvheadend service is not running"
fi

runtime_ready=0
if ldconfig -p 2>/dev/null | grep -q 'libndi\.so\.6' && \
   [ -f /var/lib/teletool/ndi-runtime-verified ]; then
  runtime_ready=1
  tt_ui_status READY "NDI SDK runtime is installed and verified"
else
  tt_ui_status ACTION "NDI SDK runtime must be uploaded in the TeleTool Web UI"
fi

printf '\n  OPEN THE UNIT WEB UI\n'
printf '  ------------------------------------------------------------------------------\n'
printf '  http://%s:8000/\n' "$ip"
printf '  ------------------------------------------------------------------------------\n'
if [ "$runtime_ready" -ne 1 ]; then
  printf '\n  Open the link and drop the ARM64 libndi.so.6 file onto the upload box.\n'
  printf '  NDI SDK: https://ndi.video/for-developers/ndi-sdk/\n'
fi
printf '\n  Future updates: sudo apt-get update && sudo apt-get upgrade\n\n'
