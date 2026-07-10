#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="tvh_ndi_bridge.service"
LEGACY_SERVICE_NAMES=("tvh-ndi-bridge.service")
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${TELETOOL_SERVICE_USER:-$(id -un)}"
SERVICE_TEMPLATE="$PROJECT_DIR/deploy/systemd/$SERVICE_NAME"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
NDI_DROP_PATH="${TELETOOL_NDI_LIB:-$SERVICE_HOME/libndi.so.6}"
NDI_HELPER_TEMPLATE="$PROJECT_DIR/deploy/ndi/teletool-install-ndi-runtime"
NDI_HELPER_PATH="/usr/local/sbin/teletool-install-ndi-runtime"
NDI_SUDOERS_PATH="/etc/sudoers.d/tvh_ndi_bridge_ndi_runtime"
NDI_VERIFICATION_MARKER="/var/lib/teletool/ndi-runtime-verified"

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required for package and systemd setup." >&2
  exit 1
fi

echo "Installing Raspberry Pi OS packages..."
sudo apt-get update
sudo apt-get install -y \
  python3-venv \
  python3-pip \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  gir1.2-gst-plugins-bad-1.0 \
  alsa-utils \
  gstreamer1.0-tools \
  gstreamer1.0-alsa \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  avahi-daemon

echo "Creating Python virtual environment..."
cd "$PROJECT_DIR"
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip wheel
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f "$PROJECT_DIR/config.json" ] && [ -f "$PROJECT_DIR/config.example.json" ]; then
  cp "$PROJECT_DIR/config.example.json" "$PROJECT_DIR/config.json"
fi

if [ ! -f "$SERVICE_TEMPLATE" ]; then
  echo "Missing $SERVICE_TEMPLATE" >&2
  exit 1
fi
if [ ! -f "$NDI_HELPER_TEMPLATE" ]; then
  echo "Missing $NDI_HELPER_TEMPLATE" >&2
  exit 1
fi

echo "Removing legacy TeleTool systemd units..."
for legacy_service in "${LEGACY_SERVICE_NAMES[@]}"; do
  sudo systemctl disable --now "$legacy_service" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/$legacy_service"
  sudo rm -f "/etc/systemd/system/multi-user.target.wants/$legacy_service"
done
sudo systemctl daemon-reload
sudo systemctl reset-failed "${LEGACY_SERVICE_NAMES[@]}" 2>/dev/null || true

echo "Installing systemd service for user '$SERVICE_USER'..."
tmp_service="$(mktemp)"
sed \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
  "$SERVICE_TEMPLATE" > "$tmp_service"

sudo install -m 0644 "$tmp_service" "/etc/systemd/system/$SERVICE_NAME"
rm -f "$tmp_service"

echo "Installing verified NDI runtime upload helper..."
tmp_ndi_helper="$(mktemp)"
ndi_drop_sed="${NDI_DROP_PATH//\\/\\\\}"
ndi_drop_sed="${ndi_drop_sed//&/\\&}"
ndi_drop_sed="${ndi_drop_sed//|/\\|}"
sed -e "s|__NDI_DROP_PATH__|$ndi_drop_sed|g" "$NDI_HELPER_TEMPLATE" > "$tmp_ndi_helper"
sudo install -o root -g root -m 0755 "$tmp_ndi_helper" "$NDI_HELPER_PATH"
rm -f "$tmp_ndi_helper"

tmp_ndi_sudoers="$(mktemp)"
{
  echo "# TeleTool NDI runtime upload permission"
  echo "# Allows only the fixed, root-owned validation/install helper with no arguments."
  echo "$SERVICE_USER ALL=(root) NOPASSWD: $NDI_HELPER_PATH \"\""
} > "$tmp_ndi_sudoers"
sudo visudo -cf "$tmp_ndi_sudoers"
sudo install -o root -g root -m 0440 "$tmp_ndi_sudoers" "$NDI_SUDOERS_PATH"
rm -f "$tmp_ndi_sudoers"

sudo usermod -aG audio,video "$SERVICE_USER" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

if [ -f "$PROJECT_DIR/install_network_privileges.sh" ]; then
  echo "Installing optional web UI network-control privileges..."
  sudo sh "$PROJECT_DIR/install_network_privileges.sh" || true
fi

echo
echo "TeleTool service status:"
systemctl --no-pager --full status "$SERVICE_NAME" || true

echo
if gst-inspect-1.0 ndisink >/dev/null 2>&1; then
  echo "NDI GStreamer plugin found."
else
  echo "WARNING: gst-inspect-1.0 ndisink did not find the NDI GStreamer plugin."
  echo "For a clean Pi install, scripts/pi_full_setup.sh builds the GStreamer plugin."
fi

if /sbin/ldconfig -p 2>/dev/null | awk '
  $1 == "libndi.so.6" { found = 1 }
  END { exit !found }
'; then
  if [ -f "$NDI_VERIFICATION_MARKER" ]; then
    echo "NDI SDK 6 runtime found and verified."
  else
    echo "WARNING: NDI SDK runtime libndi.so.6 is installed but not verified."
  fi
else
  echo "WARNING: NDI SDK runtime libndi.so.6 is not installed."
  echo "The full setup script prints the download link and exact drop path."
fi

if gst-inspect-1.0 alsasink >/dev/null 2>&1; then
  echo "ALSA GStreamer audio sink found."
else
  echo "WARNING: gst-inspect-1.0 alsasink did not find an ALSA sink plugin."
fi

echo "App URL: http://$(hostname -I | awk '{print $1}'):8000/"
