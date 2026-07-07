#!/bin/sh
# Install minimal passwordless sudo rules needed for TeleTool System > Network and Power.
# Run once on the Raspberry Pi with: sudo ./install_network_privileges.sh
set -eu
SERVICE="tvh_ndi_bridge.service"
SUDOERS="/etc/sudoers.d/tvh_ndi_bridge_network"

if [ "$(id -u)" != "0" ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

USER_NAME=""
if command -v systemctl >/dev/null 2>&1; then
  USER_NAME="$(systemctl show "$SERVICE" -p User --value 2>/dev/null || true)"
  if [ -z "$USER_NAME" ]; then
    PID="$(systemctl show "$SERVICE" -p MainPID --value 2>/dev/null || true)"
    if [ -n "$PID" ] && [ "$PID" != "0" ]; then
      USER_NAME="$(ps -o user= -p "$PID" 2>/dev/null | awk '{print $1}' || true)"
    fi
  fi
fi

if [ -z "$USER_NAME" ]; then
  USER_NAME="admin"
fi

if [ "$USER_NAME" = "root" ]; then
  echo "$SERVICE appears to run as root; no sudoers rule is required."
  exit 0
fi

NMCLI="$(command -v nmcli 2>/dev/null || true)"
CP="$(command -v cp 2>/dev/null || true)"
SYSTEMCTL="$(command -v systemctl 2>/dev/null || true)"
SHUTDOWN="$(command -v shutdown 2>/dev/null || true)"
REBOOT="$(command -v reboot 2>/dev/null || true)"

TMP="$(mktemp)"
{
  echo "# TeleTool network-control permissions"
  echo "# Allows the web UI to apply manual/DHCP network settings and reboot without an interactive password prompt."
  echo "# Installed by install_network_privileges.sh."
  if [ -n "$NMCLI" ]; then
    echo "$USER_NAME ALL=(root) NOPASSWD: $NMCLI *"
  fi
  if [ -n "$CP" ]; then
    echo "$USER_NAME ALL=(root) NOPASSWD: $CP /tmp/tvh_bridge_dhcpcd.conf /etc/dhcpcd.conf"
  fi
  if [ -n "$SYSTEMCTL" ]; then
    echo "$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL restart dhcpcd"
    echo "$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL reboot"
  fi
  if [ -n "$SHUTDOWN" ]; then
    echo "$USER_NAME ALL=(root) NOPASSWD: $SHUTDOWN -r now"
  fi
  if [ -n "$REBOOT" ]; then
    echo "$USER_NAME ALL=(root) NOPASSWD: $REBOOT"
  fi
} > "$TMP"

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$TMP"
fi
install -m 0440 "$TMP" "$SUDOERS"
rm -f "$TMP"

echo "Installed $SUDOERS for service user '$USER_NAME'."
echo "Restarting $SERVICE so future web requests see the updated privileges..."
systemctl restart "$SERVICE" 2>/dev/null || true
